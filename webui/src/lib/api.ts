import type {
  ChatSummary,
  ProviderSettingsUpdate,
  ConfigSnapshot,
  SecretEntry,
  SettingsPayload,
  SettingsUpdate,
  SlashCommand,
  WebSearchSettingsUpdate,
  WebuiThreadPersistedPayload,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

let reauthHandler: (() => Promise<string | null>) | null = null;

/** Register a callback that mints a fresh token. When a REST call gets
 *  a 401 — the gateway restarted and wiped its in-memory token pool, so
 *  the cached token is now stale — `request` calls this, then retries
 *  once. Without it, every REST call stays broken until a page reload. */
export function setApiReauthHandler(
  handler: (() => Promise<string | null>) | null,
): void {
  reauthHandler = handler;
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
  retryOn401 = true,
): Promise<T> {
  const res = await fetch(url, {
    ...(init ?? {}),
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
    credentials: "same-origin",
  });
  if (res.status === 401 && retryOn401 && reauthHandler) {
    const fresh = await reauthHandler();
    if (fresh && fresh !== token) {
      return request<T>(url, fresh, init, false);
    }
  }
  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    title: s.title ?? "",
    preview: s.preview ?? "",
  }));
}

/** Disk-backed WebUI display thread snapshot (separate from agent session). */
export async function fetchWebuiThread(
  token: string,
  key: string,
  base: string = "",
): Promise<WebuiThreadPersistedPayload | null> {
  const url = `${base}/api/sessions/${encodeURIComponent(key)}/webui-thread`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as WebuiThreadPersistedPayload;
}

export async function deleteSession(
  token: string,
  key: string,
  base: string = "",
): Promise<boolean> {
  const body = await request<{ deleted: boolean }>(
    `${base}/api/sessions/${encodeURIComponent(key)}/delete`,
    token,
  );
  return body.deleted;
}

/** P2 (doc 20): persist a user-edited title for a webui session.
 *  Backend sets ``title_user_edited`` so the LLM auto-title generator
 *  won't overwrite it on later turns. */
export async function renameSession(
  token: string,
  key: string,
  title: string,
  base: string = "",
): Promise<string> {
  const trimmed = title.trim();
  if (!trimmed) throw new ApiError(400, "title is required");
  const url =
    `${base}/api/sessions/${encodeURIComponent(key)}/rename` +
    `?title=${encodeURIComponent(trimmed)}`;
  const body = await request<{ title: string }>(url, token);
  return body.title;
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(`${base}/api/settings`, token);
}

export async function listSlashCommands(
  token: string,
  base: string = "",
): Promise<SlashCommand[]> {
  type Row = {
    command: string;
    title: string;
    description: string;
    icon: string;
    arg_hint?: string;
  };
  const body = await request<{ commands: Row[] }>(`${base}/api/commands`, token);
  return body.commands
    .filter((command) => !["/stop", "/restart"].includes(command.command))
    .map((command) => ({
      command: command.command,
      title: command.title,
      description: command.description,
      icon: command.icon,
      argHint: command.arg_hint ?? "",
    }));
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.model !== undefined) query.set("model", update.model);
  if (update.provider !== undefined) query.set("provider", update.provider);
  return request<SettingsPayload>(`${base}/api/settings/update?${query}`, token);
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.apiBase !== undefined) query.set("api_base", update.apiBase);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/update?${query}`,
    token,
  );
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.baseUrl !== undefined) query.set("base_url", update.baseUrl);
  return request<SettingsPayload>(
    `${base}/api/settings/web-search/update?${query}`,
    token,
  );
}

export async function listSecrets(
  token: string,
  base: string = "",
): Promise<SecretEntry[]> {
  const res = await request<{ secrets: SecretEntry[] }>(
    `${base}/api/secrets`,
    token,
  );
  return res.secrets;
}

// A secret's value is written over the websocket (`DurinClient.storeSecret`),
// never an HTTP query string — see durin-client.ts.

export async function deleteSecret(
  token: string,
  name: string,
  base: string = "",
): Promise<void> {
  const query = new URLSearchParams({ name });
  await request<{ ok: boolean }>(`${base}/api/secrets/delete?${query}`, token);
}

// -- cron jobs (P11) ---------------------------------------------------

export interface CronJobRow {
  id: string;
  name: string;
  enabled: boolean;
  is_system: boolean;
  schedule: {
    kind: string;
    label: string;
    expr: string | null;
    every_ms: number | null;
    at_ms: number | null;
    tz: string | null;
  };
  message: string;
  channel: string;
  state: {
    next_run_at_ms: number | null;
    last_run_at_ms: number | null;
    last_status: "ok" | "error" | "skipped" | null;
    last_error: string | null;
    executing?: boolean;
  };
  created_at_ms: number;
  updated_at_ms: number;
}

export async function listCronJobs(
  token: string,
  base: string = "",
): Promise<CronJobRow[]> {
  const res = await request<{ jobs: CronJobRow[] }>(`${base}/api/cron`, token);
  return res.jobs;
}

export async function removeCronJob(
  token: string,
  id: string,
  base: string = "",
): Promise<void> {
  const query = new URLSearchParams({ id });
  await request<{ result: string }>(`${base}/api/cron/remove?${query}`, token);
}

export async function toggleCronJob(
  token: string,
  id: string,
  enabled: boolean,
  base: string = "",
): Promise<CronJobRow> {
  const query = new URLSearchParams({ id, enabled: String(enabled) });
  const res = await request<{ job: CronJobRow }>(
    `${base}/api/cron/toggle?${query}`,
    token,
  );
  return res.job;
}

/** POST-style GET /api/cron/run?id=… — trigger a job now (background).
 * `started:false` means it was already running (overlap guard). */
export async function runCronJob(
  token: string,
  id: string,
  base: string = "",
): Promise<{ started: boolean; reason?: string }> {
  const query = new URLSearchParams({ id });
  return await request<{ started: boolean; reason?: string }>(
    `${base}/api/cron/run?${query}`,
    token,
  );
}

export async function getConfig(
  token: string,
  base: string = "",
): Promise<ConfigSnapshot> {
  return request<ConfigSnapshot>(`${base}/api/config`, token);
}

export async function setConfigValue(
  token: string,
  key: string,
  value: unknown,
  base: string = "",
): Promise<Record<string, unknown>> {
  const query = new URLSearchParams({ key, value: JSON.stringify(value) });
  const res = await request<{ ok: boolean; config: Record<string, unknown> }>(
    `${base}/api/config/set?${query}`,
    token,
  );
  return res.config;
}

// -- skills (skills-evolution-mvp) -------------------------------------------

/** A §8.C security verdict. "" = not yet scanned (quarantine without a report). */
export type SkillVerdict = "safe" | "caution" | "dangerous" | "";

export interface SkillFinding {
  category: string;
  severity: "info" | "caution" | "high" | "dangerous";
  where: string;
  detail: string;
}

export interface SkillRow {
  name: string;
  source: string;
  mode: "auto" | "manual";
  description?: string;
  provenance?: { source?: string; created_at?: string };
  status?: "active" | "quarantined";
  verdict?: SkillVerdict;
  findings?: SkillFinding[];
}

/** A skill awaiting an import decision in `.durin/import-quarantine/` (§6.B fills these). */
export interface QuarantineRow {
  name: string;
  status: "quarantined";
  source: string;
  verdict: SkillVerdict;
  findings: SkillFinding[];
  /** Suggested allowlist prefix for a one-click "trust this source" (§A1). */
  trust_prefix?: string;
  /** Declared dependency installs (info only — durin never auto-runs them, §B11). */
  install_specs?: string[];
}

export interface SkillDetail {
  name: string;
  mode: "auto" | "manual";
  content: string;
}

export async function listSkills(
  token: string,
  base: string = "",
): Promise<SkillRow[]> {
  const res = await request<{ skills: SkillRow[] }>(`${base}/api/skills`, token);
  return res.skills;
}

export async function listQuarantine(
  token: string,
  base: string = "",
): Promise<QuarantineRow[]> {
  const res = await request<{ quarantined: QuarantineRow[] }>(
    `${base}/api/skills/quarantine`,
    token,
  );
  return res.quarantined;
}

// -- skill import (§6.B) -----------------------------------------------------

export interface SkillCandidate {
  name: string;
  ref: string;
  kind: "local" | "https" | "github";
  detail: string;
}

/** Result of fetching a source into quarantine. Exactly one shape applies:
 *  a single skill landed (`quarantined`), several were found (`candidates` —
 *  pick one), or the source was fuzzy (`unresolved_reason`). */
export interface ImportResult {
  quarantined?: string;
  source?: string;
  verdict?: SkillVerdict;
  needs?: "allow" | "confirm" | "block";
  findings?: SkillFinding[];
  candidates?: SkillCandidate[];
  unresolved_reason?: string;
}

/** Outcome of an approve (install through the gate). `ok` on success;
 *  `refused` (with the verdict) when the gate blocked/needs confirmation. */
export interface ApproveResult {
  ok?: boolean;
  name?: string;
  verdict?: SkillVerdict;
  commit?: string;
  refused?: "block" | "confirm" | "invalid" | "exists";
  message?: string;
  error?: string;
}

export async function importSource(
  token: string,
  source: string,
  base: string = "",
): Promise<ImportResult> {
  const query = new URLSearchParams({ source });
  return request<ImportResult>(`${base}/api/skills/import?${query}`, token);
}

/** A registry search hit. `ref` is the importable source (feed it to
 *  `importSource`); `signals` is open — today only `installs` is read. */
export interface SkillSearchHit {
  name: string;
  ref: string;
  registry: string;
  description: string;
  signals: { installs?: number };
}

export async function searchSkills(
  token: string,
  query: string,
  limit = 0,
  base: string = "",
): Promise<{ hits: SkillSearchHit[] }> {
  const params = new URLSearchParams({ query, limit: String(limit) });
  return request<{ hits: SkillSearchHit[] }>(
    `${base}/api/skills/search?${params}`,
    token,
  );
}

export async function approveSkill(
  token: string,
  name: string,
  opts: { confirm?: boolean; override?: boolean; replace?: boolean } = {},
  base: string = "",
): Promise<ApproveResult> {
  const query = new URLSearchParams();
  if (opts.confirm) query.set("confirm", "true");
  if (opts.override) query.set("override", "true");
  if (opts.replace) query.set("replace", "true");
  const qs = query.toString();
  const url = `${base}/api/skills/${encodeURIComponent(name)}/approve${qs ? `?${qs}` : ""}`;
  // 200 (installed) and 409 (gate refused) both carry a useful body; only 5xx throws.
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status >= 500) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as ApproveResult;
}

export async function rejectSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<{ ok?: boolean; error?: string }> {
  return request<{ ok?: boolean; error?: string }>(
    `${base}/api/skills/${encodeURIComponent(name)}/reject`,
    token,
  );
}

export interface JudgeResult {
  name: string;
  verdict?: SkillVerdict;
  findings?: SkillFinding[];
  judged?: boolean;
  error?: string;
}

/** Run the LLM judge on-demand over a quarantined skill (independent of the
 *  auto-run trigger). Updates the quarantine's stored scan. */
export async function judgeSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<JudgeResult> {
  return request<JudgeResult>(
    `${base}/api/skills/${encodeURIComponent(name)}/judge`,
    token,
  );
}

export interface GithubTokenTestResult {
  ok: boolean;
  remaining?: number | null;
  limit?: number | null;
  error?: string;
}

export async function testGithubToken(
  token: string,
  secret: string,
  base: string = "",
): Promise<GithubTokenTestResult> {
  const query = new URLSearchParams({ secret });
  return request<GithubTokenTestResult>(
    `${base}/api/skills/github-token-test?${query}`,
    token,
  );
}

/** Add a trust-pattern prefix to the import allowlist (one-click "trust source").
 *  Reads the current allowlist, appends, and writes it back via config. */
export async function addTrustPattern(
  token: string,
  prefix: string,
  base: string = "",
): Promise<void> {
  const snap = await getConfig(token, base);
  const skills = (snap.config as { skills?: { security?: { allowlist?: unknown } } })?.skills;
  const cur = Array.isArray(skills?.security?.allowlist)
    ? (skills!.security!.allowlist as string[])
    : [];
  if (cur.includes(prefix)) return;
  await setConfigValue(token, "skills.security.allowlist", [...cur, prefix], base);
}

export async function getSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillDetail> {
  return request<SkillDetail>(`${base}/api/skills/${encodeURIComponent(name)}`, token);
}

export async function saveSkill(
  token: string,
  name: string,
  content: string,
  base: string = "",
): Promise<void> {
  const query = new URLSearchParams({ content });
  await request<{ ok: boolean }>(`${base}/api/skills/${encodeURIComponent(name)}/save?${query}`, token);
}

export async function setSkillMode(
  token: string,
  name: string,
  value: "auto" | "manual",
  base: string = "",
): Promise<void> {
  const query = new URLSearchParams({ value });
  await request<{ ok: boolean }>(`${base}/api/skills/${encodeURIComponent(name)}/mode?${query}`, token);
}

export interface ModelTestResult {
  status: "ok" | "warn" | "fail";
  message: string;
  fix: string;
}

export async function testModel(
  token: string,
  opts: { model?: string; provider?: string } = {},
  base: string = "",
): Promise<ModelTestResult> {
  const query = new URLSearchParams();
  if (opts.model) query.set("model", opts.model);
  if (opts.provider) query.set("provider", opts.provider);
  const qs = query.toString();
  return request<ModelTestResult>(
    `${base}/api/model/test${qs ? `?${qs}` : ""}`,
    token,
  );
}

/** Result of a cross-encoder model probe (audit B12). */
export interface CrossEncoderTestResult {
  status: "ok" | "fail";
  message: string;
  model_id: string;
  duration_ms: number;
}

/** Probe a cross-encoder model id by loading + running a trivial score
 *  against it. Returns ok/fail with a human-readable message and the
 *  load+score timing. Used by the Settings → Memory pane so an operator
 *  can verify a model id (sentence-transformers handle, HuggingFace
 *  reference, local path, etc.) before committing it to config. */
export async function testCrossEncoderModel(
  token: string,
  model: string,
  base: string = "",
): Promise<CrossEncoderTestResult> {
  const query = new URLSearchParams({ model });
  return request<CrossEncoderTestResult>(
    `${base}/api/memory/cross-encoder/test?${query.toString()}`,
    token,
  );
}

export interface ExtraStatus {
  present: boolean;
  extra: string;
  approx_size: string;
  needs_restart: boolean;
  label: string;
}

export async function getExtraStatus(
  token: string,
  feature: string,
  base: string = "",
): Promise<ExtraStatus> {
  const q = new URLSearchParams({ feature });
  return request<ExtraStatus>(`${base}/api/extras/status?${q.toString()}`, token);
}

export interface EnsureExtraResult {
  status: "present" | "installed" | "failed" | "disabled";
  needs_restart: boolean;
  message: string;
  restarting?: boolean;
}

export async function ensureExtra(
  token: string,
  feature: string,
  restart: boolean,
  base: string = "",
): Promise<EnsureExtraResult> {
  const q = new URLSearchParams({ feature, restart: String(restart) });
  return request<EnsureExtraResult>(`${base}/api/extras/ensure?${q.toString()}`, token);
}

export interface ChannelInfo {
  name: string;
  display_name: string;
  enabled: boolean;
  credential_field: string | null;
}

export async function listChannels(
  token: string,
  base: string = "",
): Promise<ChannelInfo[]> {
  const res = await request<{ channels: ChannelInfo[] }>(
    `${base}/api/channels`,
    token,
  );
  return res.channels;
}

export interface ModelCatalog {
  suggested: string[];
  models: string[];
}

export async function listModels(
  token: string,
  provider: string,
  capability: string = "",
  base: string = "",
): Promise<ModelCatalog> {
  const params = new URLSearchParams();
  if (provider) params.set("provider", provider);
  if (capability) params.set("capability", capability);
  const qs = params.toString();
  return request<ModelCatalog>(
    `${base}/api/models${qs ? `?${qs}` : ""}`,
    token,
  );
}

export interface ModelCapabilities {
  model: string;
  max_input_tokens: number | null;
  supports_vision: boolean;
  supports_audio_input: boolean;
  supports_function_calling: boolean;
}

export async function getModelCapabilities(
  token: string,
  model: string,
  provider: string,
  base: string = "",
): Promise<ModelCapabilities> {
  const query = new URLSearchParams({ model });
  if (provider) query.set("provider", provider);
  return request<ModelCapabilities>(
    `${base}/api/model/capabilities?${query}`,
    token,
  );
}

// ---------------------------------------------------------------------------
// Logs viewer (read-only): gateway + telemetry
// ---------------------------------------------------------------------------

export interface LogLineRow {
  ts: number;
  fields: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export interface LogFacets {
  levels?: string[];
  channels?: string[];
  sessions?: string[];
  types?: string[];
}

export interface LogPage {
  lines: LogLineRow[];
  facets: LogFacets;
  next_cursor: number | null;
  scanned_through_ts: number | null;
  has_more: boolean;
}

export interface LogQueryParams {
  source: "gateway" | "telemetry";
  q?: string;
  level?: string[];
  channel?: string[];
  session?: string[];
  type?: string[];
  beforeTs?: number | null;
  windowHours?: number | "all";
  limit?: number;
}

export async function fetchLogs(
  token: string,
  params: LogQueryParams,
  base: string = "",
): Promise<LogPage> {
  const sp = new URLSearchParams();
  sp.set("source", params.source);
  if (params.q) sp.set("q", params.q);
  for (const key of ["level", "channel", "session", "type"] as const) {
    const vals = params[key];
    if (vals && vals.length) sp.set(key, vals.join(","));
  }
  if (params.beforeTs != null) sp.set("before_ts", String(params.beforeTs));
  if (params.windowHours != null) sp.set("window_hours", String(params.windowHours));
  if (params.limit != null) sp.set("limit", String(params.limit));
  return request<LogPage>(`${base}/api/logs?${sp.toString()}`, token);
}

// ---------------------------------------------------------------------------
// Memory graph (Obsidian-style view)
// ---------------------------------------------------------------------------

export interface MemoryGraphNode {
  id: string;          // entity ref `<type>:<slug>`
  type: string;        // person | project | topic | …
  name: string;        // display name (frontmatter `name`)
  aliases: string[];
  weight: number;      // episodic entry count referencing this ref
  phantom?: boolean;   // tagged in entries but no consolidated page yet
}

export interface MemoryGraphEdge {
  source: string;      // node id
  target: string;      // node id
  weight: number;      // co-occurrence count in episodic entries
}

export interface MemoryGraphPayload {
  nodes: MemoryGraphNode[];
  edges: MemoryGraphEdge[];
  stats: {
    node_count: number;
    edge_count: number;
    phantom_count: number;
    truncated_nodes: boolean;
    truncated_edges: boolean;
    types: string[];
  };
}

export async function fetchMemoryGraph(
  token: string,
  base: string = "",
): Promise<MemoryGraphPayload> {
  return request<MemoryGraphPayload>(`${base}/api/memory/graph`, token);
}

export interface MemoryEntityDetail {
  ref: string;
  // null for a "phantom" entity — tagged in entries but not yet
  // consolidated into a page. The panel still renders entries + archive.
  page: {
    type: string;
    name: string;
    aliases: string[];
    identifiers: Record<string, string[] | string> | null;
    extra: Record<string, unknown>;
    body: string;
    dream_processed_through: string | null;
  } | null;
  history: Array<{
    sha: string;
    short_sha: string;
    subject: string;
    body: string;
    when: string;
    trailers: Record<string, string[]>;
  }>;
  archive: Array<{
    slug: string;
    path: string;
    name: string;
    archived_at: string | null;
    archived_reason: string | null;
    archived_into: string | null;
  }>;
  entries: Array<{
    id: string;
    valid_from: string;
    headline: string;
    summary: string;
    body: string;
    class: string;
    entities: string[];
  }>;
}

export async function fetchMemoryEntity(
  token: string,
  ref: string,
  base: string = "",
): Promise<MemoryEntityDetail | null> {
  const url = `${base}/api/memory/entity/${encodeURIComponent(ref)}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as MemoryEntityDetail;
}

export interface MemorySearchResult {
  source: string;
  uri: string;
  headline: string;
  snippet: string;
  summary?: string;
  body?: string;
  kind: string;          // "canonical" | "fragment" | "session" | "ingested"
  class_name?: string;
  valid_from?: string;
  entities?: string[];
  rendered?: string;     // marker-wrapped block for LLM-style display
}

export interface MemorySearchPayload {
  results: MemorySearchResult[];
  total: number;
  strategy: string;
  ranking: string;
}

export async function searchMemoryApi(
  token: string,
  query: string,
  opts: { scope?: string; level?: string; base?: string } = {},
): Promise<MemorySearchPayload> {
  const params = new URLSearchParams({ q: query });
  if (opts.scope) params.set("scope", opts.scope);
  if (opts.level) params.set("level", opts.level);
  const base = opts.base ?? "";
  return request<MemorySearchPayload>(
    `${base}/api/memory/search?${params}`,
    token,
  );
}

export interface MemoryEdgeDetail {
  source: string;
  target: string;
  total: number;
  entries: Array<{
    id: string;
    valid_from: string;
    headline: string;
    summary: string;
    snippet: string;
    entities: string[];
  }>;
}

export async function fetchMemoryEdge(
  token: string,
  source: string,
  target: string,
  base: string = "",
): Promise<MemoryEdgeDetail> {
  const url =
    `${base}/api/memory/edge/${encodeURIComponent(source)}/${encodeURIComponent(target)}`;
  return request<MemoryEdgeDetail>(url, token);
}

// ---------------------------------------------------------------------------
// Individual entry browse / forget / backlinks (P12)
// ---------------------------------------------------------------------------

export interface MemoryEntryDetail {
  uri: string;          // memory/<class>/<id>
  class_name: string;   // episodic | stable | corpus | session_summary
  frontmatter: {
    id: string;
    headline: string;
    summary: string;
    valid_from: string | null;
    author: string;
    entities: string[];
    source_refs: string[];
    related: string[];
  };
  body: string;
  exists: boolean;
}

export interface MemoryBacklink {
  uri: string;
  context: string;    // "source_refs" | "related" | "body" (or comma-joined)
  headline: string;
}

export interface MemoryBacklinksPayload {
  uri: string;
  backlinks: MemoryBacklink[];
  truncated: boolean;
}

/** GET /api/memory/entry?uri=… — full frontmatter + body for one entry. */
export async function fetchMemoryEntry(
  token: string,
  uri: string,
  base: string = "",
): Promise<MemoryEntryDetail | null> {
  const url = `${base}/api/memory/entry?uri=${encodeURIComponent(uri)}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as MemoryEntryDetail;
}

/** GET /api/memory/forget?uri=… — archive an entry. Returns the
 *  backend's ``{result}`` payload verbatim so the UI can branch on
 *  ``"archived" | "not_found" | "protected" | "invalid"``. */
export async function forgetMemoryEntry(
  token: string,
  uri: string,
  base: string = "",
): Promise<{ result: string; detail?: string }> {
  const url = `${base}/api/memory/forget?uri=${encodeURIComponent(uri)}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  // 200, 400, 403 all carry a JSON body with `result` — surface it
  // verbatim so the caller picks the message. Only network / 5xx
  // errors throw.
  if (res.status >= 500) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  return (await res.json()) as { result: string; detail?: string };
}

/** GET /api/memory/backlinks?uri=… — entries that reference this one. */
export async function fetchMemoryBacklinks(
  token: string,
  uri: string,
  base: string = "",
): Promise<MemoryBacklinksPayload> {
  const url = `${base}/api/memory/backlinks?uri=${encodeURIComponent(uri)}`;
  return request<MemoryBacklinksPayload>(url, token);
}

export interface MemorySessionDetail {
  session_ref: string;
  session_key: string | null;
  info: {
    title: string | null;
    message_count: number;
    channel: string | null;
    model: string | null;
    created_at: string | null;
    updated_at: string | null;
  };
  entities_tagged: {
    from_meta: string[];
    from_source_refs: string[];
  };
  events: Array<Record<string, unknown>>;
  memory_ops: Array<{
    tool: string;
    ts: string | null;
    args_preview: string;
    result_preview: string;
    msg_index: number | null;
  }>;
  recent_messages: Array<{
    role: string;
    ts: string | number | null;
    preview: string;
  }>;
  entries_linked: Array<{
    id: string;
    valid_from: string;
    headline: string;
    summary: string;
    snippet: string;
    entities: string[];
  }>;
}

export async function fetchMemorySession(
  token: string,
  stem: string,
  base: string = "",
): Promise<MemorySessionDetail | null> {
  const url = `${base}/api/memory/session/${encodeURIComponent(stem)}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as MemorySessionDetail;
}

export interface CodexStatus {
  connected: boolean;
  email?: string | null;
  plan?: string | null;
  source?: "durin" | "codex-cli";
  /** True when the webui was reached via localhost — loopback OAuth (no device-auth toggle) works. */
  can_loopback?: boolean;
}

export interface CodexDeviceChallenge {
  user_code: string;
  verification_uri: string;
  device_auth_id: string;
  interval: number;
  expires_in: number;
}

export interface CodexPollResult extends CodexStatus {
  status: "pending" | "ok" | "error";
  error?: string;
}

export async function fetchCodexStatus(
  token: string,
  base: string = "",
): Promise<CodexStatus> {
  return request<CodexStatus>(`${base}/api/oauth/codex/status`, token);
}

export async function startCodexDeviceAuth(
  token: string,
  base: string = "",
): Promise<CodexDeviceChallenge> {
  return request<CodexDeviceChallenge>(`${base}/api/oauth/codex/start`, token);
}

export async function startCodexLoopbackAuth(
  token: string,
  base: string = "",
): Promise<{ authorize_url: string }> {
  return request<{ authorize_url: string }>(
    `${base}/api/oauth/codex/start-loopback`,
    token,
    { method: "POST" },
  );
}

export async function pollCodexDeviceAuth(
  token: string,
  deviceAuthId: string,
  userCode: string,
  base: string = "",
): Promise<CodexPollResult> {
  const q = new URLSearchParams();
  q.set("device_auth_id", deviceAuthId);
  q.set("user_code", userCode);
  return request<CodexPollResult>(
    `${base}/api/oauth/codex/poll?${q}`,
    token,
  );
}

export async function disconnectCodex(
  token: string,
  base: string = "",
): Promise<CodexStatus> {
  return request<CodexStatus>(`${base}/api/oauth/codex/disconnect`, token, {
    method: "POST",
  });
}
