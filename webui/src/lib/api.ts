import type {
  ChatSummary,
  ProviderSettingsUpdate,
  ConfigSnapshot,
  McpOauthCapability,
  McpOauthLoginResult,
  McpRegistryHit,
  McpRegistryServerDetail,
  McpRuntimeStatus,
  McpServerConfig,
  McpUpdateInfo,
  McpServerDetail,
  McpServerSummary,
  SecretEntry,
  SettingsPayload,
  SettingsUpdate,
  SlashCommand,
  WebSearchSettingsUpdate,
  WebuiThreadPersistedPayload,
} from "./types";

import { fetchWithReauth } from "./http";

// Re-exported so existing call sites keep importing it from "@/lib/api"; the
// implementation (and the lone fetch global) now lives in http.ts.
export { setApiReauthHandler } from "./http";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
  retryOn401 = true,
): Promise<T> {
  const res = await fetchWithReauth(url, token, init, retryOn401);
  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

function post<T>(url: string, token: string, body: unknown): Promise<T> {
  return request<T>(url, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function del<T>(url: string, token: string, body: unknown): Promise<T> {
  return request<T>(url, token, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function patch<T>(url: string, token: string, body: unknown): Promise<T> {
  return request<T>(url, token, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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
    `${base}/api/v1/sessions`,
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

export interface BackgroundTask {
  kind: "subagent" | "workflow";
  id: string;
  label: string;
  status: "running" | "needs_input" | "done" | "failed";
  started_at: number;
  ended_at: number | null;
  session_key: string | null;
  nodes?: Array<{ id: string; label?: string; status: string; branches?: Array<{ id: string; label?: string; status: string }> | null }> | null;
  task?: string | null;
}

export async function listBackgroundTasks(
  token: string,
  session: string,
  base: string = "",
): Promise<BackgroundTask[]> {
  const body = await request<{ tasks: BackgroundTask[] }>(
    `${base}/api/v1/tasks?session=${encodeURIComponent(session)}`,
    token,
  );
  return body.tasks;
}

/** Disk-backed WebUI display thread snapshot (separate from agent session). */
export async function fetchWebuiThread(
  token: string,
  key: string,
  base: string = "",
): Promise<WebuiThreadPersistedPayload | null> {
  const url = `${base}/api/v1/sessions/${encodeURIComponent(key)}/webui-thread`;
  // Route through fetchWithReauth so an expired bootstrap token mints a fresh
  // one and retries, instead of surfacing an empty thread (the sidebar list
  // already reauths via request(); this read must match it).
  const res = await fetchWithReauth(url, token);
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  const envelope = await res.json();
  // v1 wraps the payload under {data: {...}}
  return (envelope.data ?? envelope) as WebuiThreadPersistedPayload;
}

export async function deleteSession(
  token: string,
  key: string,
  base: string = "",
): Promise<boolean> {
  const body = await del<{ deleted: boolean }>(
    `${base}/api/v1/sessions/${encodeURIComponent(key)}`,
    token,
    { key },
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
  const body = await post<{ title: string }>(
    `${base}/api/v1/sessions/${encodeURIComponent(key)}/rename`,
    token,
    { key, title: trimmed },
  );
  return body.title;
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(`${base}/api/v1/settings`, token);
}

// --- Workflows (visual editor) ---

export async function listWorkflows(
  token: string,
  base: string = "",
): Promise<string[]> {
  const body = await request<{ workflows: string[] }>(
    `${base}/api/v1/workflows`,
    token,
  );
  return body.workflows;
}

export async function getWorkflow(
  token: string,
  name: string,
  base: string = "",
): Promise<Record<string, unknown>> {
  const body = await request<{ name: string; definition: Record<string, unknown> }>(
    `${base}/api/v1/workflows/${encodeURIComponent(name)}`,
    token,
  );
  return body.definition;
}

export async function saveWorkflow(
  token: string,
  name: string,
  definition: unknown,
  base: string = "",
): Promise<void> {
  await post<{ name: string }>(
    `${base}/api/v1/workflows/${encodeURIComponent(name)}`,
    token,
    { definition },
  );
}

export async function deleteWorkflow(
  token: string,
  name: string,
  base: string = "",
): Promise<void> {
  await del<{ deleted: boolean }>(
    `${base}/api/v1/workflows/${encodeURIComponent(name)}`,
    token,
    {},
  );
}

/** Copy a workflow to a new name (to use as a starting point). Returns the created name. */
export async function duplicateWorkflow(
  token: string,
  name: string,
  target: string,
  base: string = "",
): Promise<string> {
  const body = await post<{ name: string }>(
    `${base}/api/v1/workflows/${encodeURIComponent(name)}/duplicate`,
    token,
    { target },
  );
  return body.name;
}

// One per-node entry in a run's trace. The attribution fields make a run auditable:
// session_key points at the fresh session that produced the row; worker_index/branch_id
// identify a fan-out worker or a static parallel branch so concurrent units stay legible;
// status is "ok" | "persist_failed" | "node_failed".
export type WorkflowRunNode = {
  node_id: string;
  iteration: number;
  passed: boolean | null;
  output: string;
  session_key: string | null;
  worker_index: number | null;
  branch_id?: string | null;
  status: string;
  route_label: string | null;
};

export type WorkflowRunResult = {
  status: string;
  final_output: string;
  run_id: string;
  runs: WorkflowRunNode[];
  output_dir?: string;
  exhausted_node?: string;
};

export async function runWorkflow(
  token: string,
  name: string,
  task: string,
  inputFiles: string[] = [],
  outputFormat: string = "",
  base: string = "",
): Promise<WorkflowRunResult> {
  const body: { task: string; input_files?: string[]; output_format?: string } = { task };
  if (inputFiles.length > 0) body.input_files = inputFiles;
  if (outputFormat.trim()) body.output_format = outputFormat.trim();
  return post<WorkflowRunResult>(
    `${base}/api/v1/workflows/${encodeURIComponent(name)}/run`,
    token,
    body,
  );
}

export type WorkflowRecommendation = {
  id: string;
  target_id: string;
  field: string;
  current: string;
  proposed: string;
  reason: string;
};

export async function getWorkflowRecommendations(
  token: string,
  name: string,
  base: string = "",
): Promise<WorkflowRecommendation[]> {
  const body = await request<{ recommendations: WorkflowRecommendation[] }>(
    `${base}/api/v1/workflows/${encodeURIComponent(name)}/recommendations`,
    token,
  );
  return body.recommendations;
}

export async function applyWorkflowRecommendation(
  token: string,
  name: string,
  id: string,
  base: string = "",
): Promise<{ ok: boolean; detail: string }> {
  return post<{ ok: boolean; detail: string }>(
    `${base}/api/v1/workflows/${encodeURIComponent(name)}/recommendations/${encodeURIComponent(id)}/apply`,
    token,
    {},
  );
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
  const body = await request<{ commands: Row[] }>(`${base}/api/v1/commands`, token);
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

/** A registered agent mode (built-in or user-defined). `icon` is optional
 *  mode-supplied data; the picker falls back to a generic glyph when it is null,
 *  so nothing is hardcoded per mode name. `allowed === null` means full access
 *  (subject to `denied`); a list means only those tools. `builtin` modes are
 *  read-only in the settings UI. */
export interface ModeInfo {
  name: string;
  description: string;
  icon: string | null;
  builtin: boolean;
  allowed: string[] | null;
  denied: string[];
  prompt_suffix: string;
}

export interface ModeUpsert {
  name: string;
  description?: string;
  allowed?: string[] | null;
  denied?: string[];
  prompt_suffix?: string;
  icon?: string | null;
}

export async function listModes(
  token: string,
  base: string = "",
): Promise<ModeInfo[]> {
  const body = await request<{ modes: ModeInfo[] }>(`${base}/api/v1/modes`, token);
  return body.modes;
}

export async function upsertMode(
  token: string,
  mode: ModeUpsert,
  base: string = "",
): Promise<ModeInfo> {
  const body = await post<{ mode: ModeInfo }>(`${base}/api/v1/modes`, token, mode);
  return body.mode;
}

export async function deleteMode(
  token: string,
  name: string,
  base: string = "",
): Promise<boolean> {
  const body = await del<{ ok: boolean }>(`${base}/api/v1/modes`, token, { name });
  return body.ok;
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const body: Record<string, string> = {};
  if (update.model !== undefined) body.model = update.model;
  if (update.provider !== undefined) body.provider = update.provider;
  return post<SettingsPayload>(`${base}/api/v1/settings`, token, body);
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const body: Record<string, string | null> = { provider: update.provider };
  if (update.apiKey !== undefined) body.apiKey = update.apiKey;
  if (update.apiBase !== undefined) body.apiBase = update.apiBase;
  return post<SettingsPayload>(`${base}/api/v1/settings/provider`, token, body);
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const body: Record<string, string | null> = { provider: update.provider };
  if (update.apiKey !== undefined) body.apiKey = update.apiKey;
  if (update.baseUrl !== undefined) body.baseUrl = update.baseUrl;
  return post<SettingsPayload>(`${base}/api/v1/settings/web-search`, token, body);
}

export async function listSecrets(
  token: string,
  base: string = "",
): Promise<SecretEntry[]> {
  const res = await request<{ secrets: SecretEntry[] }>(
    `${base}/api/v1/secrets`,
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
  await del<{ ok: boolean }>(`${base}/api/v1/secrets`, token, { name });
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
  mode: string;
  model: string | null;
  persona: string | null;
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
  run_history?: Array<{
    run_at_ms: number;
    status: "ok" | "error" | "skipped";
    duration_ms: number;
    error: string | null;
    session_key: string | null;
    model: string | null;
    persona: string | null;
    summary: string | null;
  }>;
}

export async function listCronJobs(
  token: string,
  base: string = "",
): Promise<CronJobRow[]> {
  const res = await request<{ jobs: CronJobRow[] }>(`${base}/api/v1/cron`, token);
  return res.jobs;
}

export async function removeCronJob(
  token: string,
  id: string,
  base: string = "",
): Promise<void> {
  await del<{ result: string }>(`${base}/api/v1/cron`, token, { id });
}

export async function toggleCronJob(
  token: string,
  id: string,
  enabled: boolean,
  base: string = "",
): Promise<CronJobRow> {
  const res = await post<{ job: CronJobRow }>(
    `${base}/api/v1/cron/toggle`,
    token,
    { id, enabled },
  );
  return res.job;
}

/** Trigger a job now (background).
 * `started:false` means it was already running (overlap guard). */
export async function runCronJob(
  token: string,
  id: string,
  base: string = "",
): Promise<{ started: boolean; reason?: string }> {
  return post<{ started: boolean; reason?: string }>(
    `${base}/api/v1/cron/run`,
    token,
    { id },
  );
}

export async function addCronJob(
  token: string,
  body: {
    name: string; message: string; mode: string; model: string | null;
    schedule_kind: string; expr?: string | null; every_ms?: number | null;
    at_ms?: number | null; tz?: string | null;
    deliver: boolean; channel?: string | null; to?: string | null;
  },
  base = "",
): Promise<CronJobRow> {
  const res = await post<{ job: CronJobRow }>(`${base}/api/v1/cron`, token, body);
  return res.job;
}

export async function updateCronJob(
  token: string,
  body: { id: string } & Partial<Parameters<typeof addCronJob>[1]>,
  base = "",
): Promise<CronJobRow> {
  const res = await patch<{ job: CronJobRow }>(`${base}/api/v1/cron`, token, body);
  return res.job;
}

export async function getConfig(
  token: string,
  base: string = "",
): Promise<ConfigSnapshot> {
  return request<ConfigSnapshot>(`${base}/api/v1/config`, token);
}

export async function setConfigValue(
  token: string,
  key: string,
  value: unknown,
  base: string = "",
): Promise<Record<string, unknown>> {
  const res = await post<{ ok: boolean; config: Record<string, unknown> }>(
    `${base}/api/v1/config`,
    token,
    { key, value: JSON.stringify(value) },
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

/** A user/LLM "Revisada" override that cleared a flagged active skill. */
export interface SkillReview {
  by: "user" | "llm";
  verdict: string;
  original: string;
  note: string;
  at: string;
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
  /** Present when a review override is in effect (verdict/findings preserved). */
  review?: SkillReview;
  /** Whether/how this skill can be removed: "remove" (workspace skill),
   *  "revert" (forked builtin → shipped version), or null/absent (pure builtin). */
  removable?: "remove" | "revert" | null;
  requirements?: SkillRequirements | null;
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
  /** Gate outcome: allow installs straight away; confirm/block need a prompt. */
  needs?: "allow" | "confirm" | "block";
  /** Why approval is required, in structured form (rendered as plain language). */
  reasons?: { code: string; detail?: string }[];
  requirements?: SkillRequirements | null;
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
  const res = await request<{ data: { skills: SkillRow[] } }>(`${base}/api/v1/skills`, token);
  return res.data.skills;
}

export async function listQuarantine(
  token: string,
  base: string = "",
): Promise<QuarantineRow[]> {
  const res = await request<{ data: { quarantined: QuarantineRow[] } }>(
    `${base}/api/v1/skills/quarantine`,
    token,
  );
  return res.data.quarantined;
}

// -- skill import (§6.B) -----------------------------------------------------

export interface SkillCandidate {
  name: string;
  ref: string;
  kind: "local" | "https" | "github";
  detail: string;
}

/** Result of an import. Exactly one shape applies:
 *  - `installed` — the gate cleared it (`allow`) and it was auto-installed;
 *  - `already_installed` — present locally; re-import with `replace` to override;
 *  - `quarantined` — needs a decision (`needs` = confirm/block);
 *  - `candidates` — several skills found (pick one);
 *  - `unresolved_reason` — the source was fuzzy. */
export interface ImportResult {
  installed?: string;
  already_installed?: string;
  quarantined?: string;
  source?: string;
  verdict?: SkillVerdict;
  needs?: "allow" | "confirm" | "block";
  commit?: string;
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
  deps_results?: Array<{ command: string; success: boolean; output?: string; error?: string }>;
}

export async function importSource(
  token: string,
  source: string,
  base: string = "",
  replace = false,
): Promise<ImportResult> {
  const res = await post<{ data: ImportResult }>(
    `${base}/api/v1/skills/import`,
    token,
    { source, replace },
  );
  return res.data;
}

/** A registry search hit. `ref` is the importable source (feed it to
 *  `importSource`); `signals` is open — today only `installs` is read. */
export interface SkillSearchHit {
  name: string;
  ref: string;
  registry: string;
  description: string;
  signals: { installs?: number };
  installed?: boolean;
}

export async function searchSkills(
  token: string,
  query: string,
  limit = 0,
  base: string = "",
): Promise<{ hits: SkillSearchHit[] }> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const res = await request<{ data: { hits: SkillSearchHit[] } }>(
    `${base}/api/v1/skills/search?${params}`,
    token,
  );
  return res.data;
}

export interface SkillDescribeResult {
  ref: string;
  description: string;
  body?: string;
  platforms?: string[] | null;
  requires?: { bins: string[]; env: string[] } | null;
}

/** Lazy SKILL.md description peek for a registry hit (search UI, on expand).
 *  Returns an empty description when none is available — never throws on 404. */
export async function describeSkill(
  token: string,
  ref: string,
  base: string = "",
): Promise<SkillDescribeResult> {
  const params = new URLSearchParams({ ref });
  try {
    const res = await request<{ data: SkillDescribeResult }>(
      `${base}/api/v1/skills/describe?${params}`,
      token,
    );
    return res.data;
  } catch {
    return { ref, description: "" };
  }
}

export interface RequirementBin {
  name: string;
  available: boolean;
  installable?: boolean;
  install_spec?: string;
}

export interface RequirementEnv {
  name: string;
  available: boolean;
}

export interface SkillRequirements {
  platforms: string[];
  platform_ok: boolean;
  bins: RequirementBin[];
  env: RequirementEnv[];
  compatibility: string;
}

export async function installSkillDeps(
  token: string,
  name: string,
  bin?: string,
  base: string = "",
): Promise<{ ok?: boolean; results?: Array<{ command: string; success: boolean; output?: string; error?: string }>; error?: string }> {
  const body: Record<string, string> = { name };
  if (bin) body.binName = bin;
  const res = await fetchWithReauth(`${base}/api/v1/skills/${encodeURIComponent(name)}/install-deps`, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status >= 500) throw new ApiError(res.status, `HTTP ${res.status}`);
  const envelope = await res.json();
  // 2xx → data; a 4xx is problem+json with the payload under details.
  return envelope.details ?? envelope.data ?? envelope;
}

export async function approveSkill(
  token: string,
  name: string,
  opts: { confirm?: boolean; override?: boolean; replace?: boolean; install_deps?: boolean } = {},
  base: string = "",
): Promise<ApproveResult> {
  const body: Record<string, unknown> = { name };
  if (opts.confirm) body.confirm = true;
  if (opts.override) body.override = true;
  if (opts.replace) body.replace = true;
  if (opts.install_deps) body.installDeps = true;
  // 2xx carries the result in `data`; a 409 (gate refused) is problem+json with
  // the gate payload under `details`; only 5xx throws.
  const res = await fetchWithReauth(`${base}/api/v1/skills/${encodeURIComponent(name)}/approve`, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status >= 500) throw new ApiError(res.status, `HTTP ${res.status}`);
  const envelope = await res.json();
  return (envelope.details ?? envelope.data ?? envelope) as ApproveResult;
}

export async function rejectSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<{ ok?: boolean; error?: string }> {
  const res = await del<{ data: { ok?: boolean; error?: string } }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/quarantine`,
    token,
    { name },
  );
  return res.data ?? res;
}

export interface RemoveResult {
  ok?: boolean;
  name?: string;
  action?: "remove" | "revert";
  commit?: string;
  error?: string;
}

export async function removeSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<RemoveResult> {
  const res = await del<{ data: RemoveResult }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}`,
    token,
    { name },
  );
  return res.data ?? res;
}

export interface JudgeResult {
  name: string;
  verdict?: SkillVerdict;
  findings?: SkillFinding[];
  judged?: boolean;
  error?: string;
  summary?: string;
  error_code?: "unreachable" | "parse" | "no_model";
}

/** Run the LLM judge on-demand over a quarantined skill (independent of the
 *  auto-run trigger). Updates the quarantine's stored scan. */
export async function judgeSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<JudgeResult> {
  const res = await request<{ data: JudgeResult }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/judge`,
    token,
  );
  return res.data;
}

export interface SkillReviewResult {
  name: string;
  reviewed: boolean;
  review?: SkillReview;
  verdict?: SkillVerdict;
  findings?: SkillFinding[];
  error?: string;
}

/** Mark an active skill reviewed (user override to safe). */
export async function reviewSkill(
  token: string,
  name: string,
  note: string,
  base: string = "",
): Promise<SkillReviewResult> {
  const res = await post<{ data: SkillReviewResult }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/review`,
    token,
    { name, note },
  );
  return res.data;
}

/** Reopen (drop) an active skill's review. */
export async function unreviewSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillReviewResult> {
  const res = await del<{ data: SkillReviewResult }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/review`,
    token,
    { name },
  );
  return res.data ?? res;
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
  const res = await request<{ data: GithubTokenTestResult }>(
    `${base}/api/v1/skills/github-token-test?${query}`,
    token,
  );
  return res.data;
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
  const res = await request<{ data: SkillDetail }>(`${base}/api/v1/skills/${encodeURIComponent(name)}`, token);
  return res.data;
}

export async function saveSkill(
  token: string,
  name: string,
  content: string,
  base: string = "",
): Promise<void> {
  await post<{ data: { ok: boolean } }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/save`,
    token,
    { name, content },
  );
}

export async function setSkillMode(
  token: string,
  name: string,
  value: "auto" | "manual",
  base: string = "",
): Promise<void> {
  await post<{ data: { ok: boolean } }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/mode`,
    token,
    { name, value },
  );
}

export interface SkillFile {
  path: string;
  text: boolean;
  size: number;
}

export interface SkillFileContent {
  path: string;
  text: boolean;
  content: string;
}

/** Success: ok+commit (+verdict/findings from the re-scan). Failure: error.
 *  A blocked script save returns error="syntax" with lang/detail/line. */
export interface SaveFileResult {
  ok?: boolean;
  name?: string;
  path?: string;
  commit?: string;
  verdict?: SkillVerdict;
  findings?: SkillFinding[];
  error?: string;
  lang?: "python" | "bash";
  detail?: string;
  line?: number;
}

export interface SkillHistoryEntry {
  sha: string;
  timestamp: string;
  subject: string;
  actor: "user" | "agent" | "curation" | "import" | "system";
  session: string | null;
  agent: string | null;
}

export interface SkillHistory {
  provenance: { source?: string; created_at?: string; verdict?: string; fused_from?: string[] };
  commits: SkillHistoryEntry[];
}

export async function listSkillFiles(
  token: string, name: string, base: string = "",
): Promise<SkillFile[]> {
  const res = await request<{ data: { files: SkillFile[] } }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/files`, token);
  return res.data.files;
}

export async function getSkillFile(
  token: string, name: string, path: string, base: string = "",
): Promise<SkillFileContent> {
  const query = new URLSearchParams({ path });
  const res = await request<{ data: SkillFileContent }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/file?${query}`, token);
  return res.data;
}

export async function saveSkillFile(
  token: string, name: string, path: string, content: string, base: string = "",
): Promise<SaveFileResult> {
  // 2xx carries the result in `data`; a 4xx (syntax / manual-gate) is problem+json
  // with the payload under `details`; only 5xx throws.
  const res = await fetchWithReauth(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/file/save`,
    token,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path, content }),
    });
  if (res.status >= 500) throw new ApiError(res.status, `HTTP ${res.status}`);
  const envelope = await res.json();
  return (envelope.details ?? envelope.data ?? envelope) as SaveFileResult;
}

export async function getSkillHistory(
  token: string, name: string, base: string = "",
): Promise<SkillHistory> {
  const res = await request<{ data: SkillHistory }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/history`, token);
  return res.data;
}

export async function fetchSkillCommitDiff(
  token: string,
  name: string,
  sha: string,
  base: string = "",
): Promise<{ sha: string; patch: string }> {
  return request<{ sha: string; patch: string }>(
    `${base}/api/v1/skills/${encodeURIComponent(name)}/commit/${encodeURIComponent(sha)}/diff`,
    token,
  );
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
    `${base}/api/v1/model/test${qs ? `?${qs}` : ""}`,
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
    `${base}/api/v1/memory/cross-encoder/test?${query.toString()}`,
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
  return request<ExtraStatus>(
    `${base}/api/v1/extras/status?${q.toString()}`,
    token,
  );
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
  return post<EnsureExtraResult>(
    `${base}/api/v1/extras/ensure`,
    token,
    { feature, restart },
  );
}

// -- souls + personas -------------------------------------------------------

export type SoulItem = components["schemas"]["SoulItem"];
export type PersonaItem = components["schemas"]["PersonaItem"];

type components = import("./api-types").components;

export async function listSouls(
  token: string,
  base: string = "",
): Promise<SoulItem[]> {
  const res = await request<{ souls: SoulItem[] }>(`${base}/api/v1/souls`, token);
  return res.souls;
}

export async function saveSoul(
  token: string,
  body: components["schemas"]["SoulUpsertCommand"],
  base: string = "",
): Promise<SoulItem> {
  const res = await post<{ soul: SoulItem }>(`${base}/api/v1/souls`, token, body);
  return res.soul;
}

export async function deleteSoul(
  token: string,
  slug: string,
  base: string = "",
): Promise<void> {
  await del<{ ok: boolean }>(`${base}/api/v1/souls`, token, { slug });
}

export async function listPersonas(
  token: string,
  base: string = "",
): Promise<{ personas: PersonaItem[]; default: string | null }> {
  return request<{ personas: PersonaItem[]; default: string | null }>(
    `${base}/api/v1/personas`,
    token,
  );
}

export async function savePersona(
  token: string,
  body: components["schemas"]["PersonaUpsertCommand"],
  base: string = "",
): Promise<PersonaItem> {
  const res = await post<{ persona: PersonaItem }>(`${base}/api/v1/personas`, token, body);
  return res.persona;
}

export async function deletePersona(
  token: string,
  name: string,
  base: string = "",
): Promise<void> {
  await del<{ ok: boolean }>(`${base}/api/v1/personas`, token, { name });
}

export async function setDefaultPersona(
  token: string,
  name: string | null,
  base: string = "",
): Promise<void> {
  await post<{ default: string | null }>(`${base}/api/v1/personas/default`, token, { name });
}

export async function testPersona(
  token: string,
  body: { model: string | null; soul: string | null },
  base: string = "",
): Promise<components["schemas"]["PersonaTestResult"]> {
  return post<components["schemas"]["PersonaTestResult"]>(
    `${base}/api/v1/personas/test`,
    token,
    body,
  );
}

export interface ChannelField {
  name: string;
  type: "string" | "int" | "bool" | "string_list" | "secret";
  secret: boolean;
  group: string;
  required: boolean;
  default: unknown;
}

export interface ChannelInfo {
  name: string;
  display_name: string;
  enabled: boolean;
  always_on: boolean;
  description: string;
  credential_field: string | null;
  fields: ChannelField[];
}

export async function listChannels(
  token: string,
  base: string = "",
): Promise<ChannelInfo[]> {
  const res = await request<{ channels: ChannelInfo[] }>(
    `${base}/api/v1/channels`,
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
    `${base}/api/v1/models${qs ? `?${qs}` : ""}`,
    token,
  );
}

export interface PickerEntry {
  name: string;
  provider: string;
  group: string;
  role: string;
  ref: string;
  max_input_tokens?: number | null;
  supports_vision?: boolean;
  supports_audio_input?: boolean;
  supports_reasoning?: boolean;
}

export async function fetchModelPicker(
  token: string,
  recent: string[],
  base: string = "",
): Promise<PickerEntry[]> {
  const params = new URLSearchParams();
  if (recent.length) params.set("recent", recent.join(","));
  const qs = params.toString();
  const res = await request<{ entries: PickerEntry[] }>(
    `${base}/api/v1/model/picker${qs ? `?${qs}` : ""}`,
    token,
  );
  return res.entries;
}

export interface ProviderModelEntry {
  id: string;
  configured: boolean;
  max_input_tokens?: number | null;
  // Effective (override-or-catalog) caps, for the caps badge.
  supports_vision?: boolean;
  supports_audio_input?: boolean;
  supports_reasoning?: boolean;
  // Raw override (null = inherit), for seeding the tri-state editor selectors.
  supports_vision_override?: boolean | null;
  supports_audio_input_override?: boolean | null;
  supports_reasoning_override?: boolean | null;
  max_tokens?: number | null;
  context_window_tokens?: number | null;
  temperature?: number | null;
  reasoning_effort?: string | null;
  top_p?: number | null;
  top_k?: number | null;
  repeat_penalty?: number | null;
}

export interface ProviderModelParams {
  max_tokens?: number | null;
  context_window_tokens?: number | null;
  temperature?: number | null;
  reasoning_effort?: string | null;
  top_p?: number | null;
  top_k?: number | null;
  repeat_penalty?: number | null;
  supports_vision?: boolean | null;
  supports_audio_input?: boolean | null;
  supports_reasoning?: boolean | null;
}

export async function fetchProviderModels(
  token: string,
  provider: string,
  base: string = "",
): Promise<ProviderModelEntry[]> {
  const res = await request<{ provider: string; models: ProviderModelEntry[] }>(
    `${base}/api/v1/providers/models?provider=${encodeURIComponent(provider)}`,
    token,
  );
  return res.models;
}

export async function upsertProviderModel(
  token: string,
  provider: string,
  model: string,
  params: ProviderModelParams,
  base: string = "",
): Promise<void> {
  await post<{ ok: boolean }>(`${base}/api/v1/providers/model`, token, {
    provider,
    model,
    ...params,
  });
}

export async function removeProviderModel(
  token: string,
  provider: string,
  model: string,
  base: string = "",
): Promise<void> {
  await post<{ ok: boolean }>(`${base}/api/v1/providers/model/remove`, token, {
    provider,
    model,
  });
}

export interface ModelCapabilities {
  model: string;
  max_input_tokens: number | null;
  supports_vision: boolean;
  supports_audio_input: boolean;
  supports_function_calling: boolean;
  supports_reasoning?: boolean;
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
    `${base}/api/v1/model/capabilities?${query}`,
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
  if (params.beforeTs != null) sp.set("beforeTs", String(params.beforeTs));
  if (params.windowHours != null) sp.set("windowHours", String(params.windowHours));
  if (params.limit != null) sp.set("limit", String(params.limit));
  return request<LogPage>(`${base}/api/v1/logs?${sp.toString()}`, token);
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
  const res = await request<{ data: MemoryGraphPayload }>(`${base}/api/v1/memory/graph`, token);
  return res.data;
}

/** Ego-graph (focus mode): a node + its N-hop neighbourhood, uncapped, so
 *  any node — including one dropped by the global cap or reached via search —
 *  can be centred with just its relations around it. */
export async function fetchMemorySubgraph(
  token: string,
  ref: string,
  opts: { hops?: number; base?: string } = {},
): Promise<MemoryGraphPayload> {
  const base = opts.base ?? "";
  const params = new URLSearchParams({ ref });
  if (opts.hops) params.set("hops", String(opts.hops));
  const res = await request<{ data: MemoryGraphPayload }>(
    `${base}/api/v1/memory/subgraph?${params}`,
    token,
  );
  return res.data;
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
    author?: string;
    created_at?: string | null;
    updated_at?: string | null;
    relations?: Array<{ to: string; type: string }>;
    // Source documents this entity was distilled from (reference:<slug>).
    derived_from?: string[];
  } | null;
  // Per-field provenance flattened into UI events: who/when/from-which
  // session each relation or attribute came from. `session_stem` + `turn`
  // are parsed from `source_ref` so the origin can be made clickable.
  provenance: Array<{
    kind: "relation" | "attribute" | "derived_from";
    detail: string | null;
    author: string | null;
    when: string | null;
    source_ref: string | null;
    session_stem: string | null;
    turn: number | null;
  }>;
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
  const url = `${base}/api/v1/memory/entity/${encodeURIComponent(ref)}`;
  const res = await fetchWithReauth(url, token);
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  const envelope = await res.json();
  return (envelope.data ?? envelope) as MemoryEntityDetail;
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
  opts: { scope?: string; level?: string; kinds?: string; base?: string } = {},
): Promise<MemorySearchPayload> {
  const params = new URLSearchParams({ q: query });
  if (opts.scope) params.set("scope", opts.scope);
  if (opts.level) params.set("level", opts.level);
  if (opts.kinds) params.set("kinds", opts.kinds);
  const base = opts.base ?? "";
  const res = await request<{ data: MemorySearchPayload }>(
    `${base}/api/v1/memory/search?${params}`,
    token,
  );
  return res.data;
}

export interface DreamEvent {
  at_ms: number;
  kind: string;
  ref: string | null;
  ref_kind: string | null;
  summary: string;
}

export interface DreamLastRun {
  at_ms: number;
  sessions: number;
  entities: number;
  merged: number;
  skills_created: number;
  skills_improved: number;
}

export interface DreamDigest {
  events: DreamEvent[];
  last_run: DreamLastRun | null;
  last_run_at_ms: number | null;
}

export async function fetchDreamDigest(
  token: string,
  limit?: number,
  base: string = "",
): Promise<DreamDigest> {
  const params = new URLSearchParams();
  if (limit != null) params.set("limit", String(limit));
  const qs = params.toString();
  const url = `${base}/api/v1/memory/dream/digest${qs ? `?${qs}` : ""}`;
  // The endpoint returns the DreamDigest fields directly (no `data` envelope —
  // same as flagged-pairs). Returning `res.data` here yielded `undefined`, so
  // the digest never reached the UI no matter what the backend produced.
  return request<DreamDigest>(url, token);
}

export type FlaggedPair = components["schemas"]["FlaggedPair"];

export async function fetchFlaggedPairs(
  token: string,
  base: string = "",
): Promise<FlaggedPair[]> {
  const res = await request<{ pairs: FlaggedPair[] }>(
    `${base}/api/v1/memory/flagged-pairs`,
    token,
  );
  return res.pairs;
}

export async function resolveFlaggedPair(
  token: string,
  body: { ref_a: string; ref_b: string; action: "merge" | "separate" },
  base: string = "",
): Promise<{ ok: boolean; action: string }> {
  return post<{ ok: boolean; action: string }>(
    `${base}/api/v1/memory/flagged-pairs/resolve`,
    token,
    body,
  );
}

export type SkillSuggestion = components["schemas"]["SkillSuggestion"];

export async function fetchSkillSuggestions(
  token: string,
  base: string = "",
): Promise<SkillSuggestion[]> {
  const res = await request<{ suggestions: SkillSuggestion[] }>(
    `${base}/api/v1/skills/suggestions`,
    token,
  );
  return res.suggestions;
}

export async function acceptSkillSuggestion(
  token: string,
  id: string,
  base: string = "",
): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(
    `${base}/api/v1/skills/suggestions/${encodeURIComponent(id)}/accept`,
    token,
    {},
  );
}

export async function rejectSkillSuggestion(
  token: string,
  id: string,
  base: string = "",
): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(
    `${base}/api/v1/skills/suggestions/${encodeURIComponent(id)}/reject`,
    token,
    {},
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
    `${base}/api/v1/memory/edge/${encodeURIComponent(source)}/${encodeURIComponent(target)}`;
  const res = await request<{ data: MemoryEdgeDetail }>(url, token);
  return res.data;
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

/** GET /api/v1/memory/entry?uri=… — full frontmatter + body for one entry. */
export async function fetchMemoryEntry(
  token: string,
  uri: string,
  base: string = "",
): Promise<MemoryEntryDetail | null> {
  const url = `${base}/api/v1/memory/entry?uri=${encodeURIComponent(uri)}`;
  const res = await fetchWithReauth(url, token);
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  const envelope = await res.json();
  return (envelope.data ?? envelope) as MemoryEntryDetail;
}

/** DELETE /api/v1/memory/entry with body {uri} — archive an entry. Returns
 *  ``{result, detail?}``: a 2xx is ``{result: "archived"}``; a failure is
 *  problem+json (403 protected / 404 not_found / 422 invalid) whose outcome is
 *  read from ``details.result`` — so the UI still branches on
 *  ``"archived" | "not_found" | "protected" | "invalid"``. */
export async function forgetMemoryEntry(
  token: string,
  uri: string,
  base: string = "",
): Promise<{ result: string; detail?: string }> {
  const url = `${base}/api/v1/memory/entry`;
  const res = await fetchWithReauth(url, token, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uri }),
  });
  // 2xx → {result: "archived"}; a 4xx is problem+json whose domain outcome is
  // under details.result. Only network / 5xx errors throw.
  if (res.status >= 500) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  const body = await res.json();
  if (res.ok) return { result: body.result };
  return { result: body.details?.result ?? "invalid", detail: body.detail };
}

/** GET /api/v1/memory/backlinks?uri=… — entries that reference this one. */
export async function fetchMemoryBacklinks(
  token: string,
  uri: string,
  base: string = "",
): Promise<MemoryBacklinksPayload> {
  const url = `${base}/api/v1/memory/backlinks?uri=${encodeURIComponent(uri)}`;
  const res = await request<{ data: MemoryBacklinksPayload }>(url, token);
  return res.data;
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
    index?: number;
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
  const url = `${base}/api/v1/memory/session/${encodeURIComponent(stem)}`;
  const res = await fetchWithReauth(url, token);
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  const envelope = await res.json();
  return (envelope.data ?? envelope) as MemorySessionDetail;
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
  return request<CodexStatus>(`${base}/api/v1/oauth/codex/status`, token);
}

export async function startCodexDeviceAuth(
  token: string,
  base: string = "",
): Promise<CodexDeviceChallenge> {
  return post<CodexDeviceChallenge>(`${base}/api/v1/oauth/codex/start`, token, {});
}

export async function startCodexLoopbackAuth(
  token: string,
  base: string = "",
): Promise<{ authorize_url: string }> {
  return post<{ authorize_url: string }>(
    `${base}/api/v1/oauth/codex/start-loopback`,
    token,
    { isLocal: false },
  );
}

export async function pollCodexDeviceAuth(
  token: string,
  deviceAuthId: string,
  userCode: string,
  base: string = "",
): Promise<CodexPollResult> {
  const q = new URLSearchParams();
  q.set("deviceAuthId", deviceAuthId);
  q.set("userCode", userCode);
  return request<CodexPollResult>(
    `${base}/api/v1/oauth/codex/poll?${q}`,
    token,
  );
}

export async function disconnectCodex(
  token: string,
  base: string = "",
): Promise<CodexStatus> {
  return del<CodexStatus>(`${base}/api/v1/oauth/codex`, token, {});
}

// --- MCP server management -------------------------------------------------

function mcpPath(name: string, suffix = ""): string {
  return `/api/v1/mcp/servers/${encodeURIComponent(name)}${suffix}`;
}

export async function searchMcpRegistry(
  token: string,
  q: string,
  limit = 10,
  base: string = "",
): Promise<{ hits: McpRegistryHit[]; more: McpRegistryHit[] }> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  const res = await request<{ hits: McpRegistryHit[]; more?: McpRegistryHit[] }>(
    `${base}/api/v1/mcp/registry/search?${params}`,
    token,
  );
  return { hits: res.hits, more: res.more ?? [] };
}

export async function describeMcpRegistryServer(
  token: string,
  ref: string,
  base: string = "",
): Promise<McpRegistryServerDetail> {
  return request<McpRegistryServerDetail>(
    `${base}/api/v1/mcp/registry/describe?ref=${encodeURIComponent(ref)}`,
    token,
  );
}

export async function installMcpFromRegistry(
  token: string,
  ref: string,
  prefer: "remote" | "local",
  envValues: Record<string, string>,
  authMethod: "" | "oauth" | "token" = "",
  base: string = "",
): Promise<McpServerDetail> {
  return post<McpServerDetail>(`${base}/api/v1/mcp/registry/install`, token, {
    ref,
    prefer,
    env_values: envValues,
    auth_method: authMethod,
  });
}

export async function mcpRegistryOauthCapability(
  token: string,
  ref: string,
  base: string = "",
): Promise<McpOauthCapability> {
  return request<McpOauthCapability>(
    `${base}/api/v1/mcp/registry/oauth-capability?ref=${encodeURIComponent(ref)}`,
    token,
  );
}

export async function mcpRegistryRuntime(
  token: string,
  ref: string,
  prefer: "remote" | "local",
  base: string = "",
): Promise<McpRuntimeStatus> {
  const params = new URLSearchParams({ ref, prefer });
  return request<McpRuntimeStatus>(
    `${base}/api/v1/mcp/registry/runtime?${params}`,
    token,
  );
}

export async function listMcpUpdates(
  token: string,
  base: string = "",
): Promise<McpUpdateInfo[]> {
  const res = await request<{ updates: McpUpdateInfo[] }>(
    `${base}/api/v1/mcp/registry/updates`,
    token,
  );
  return res.updates;
}

export async function updateMcpFromRegistry(
  token: string,
  name: string,
  base: string = "",
): Promise<McpServerDetail> {
  return post<McpServerDetail>(
    `${base}/api/v1/mcp/servers/${encodeURIComponent(name)}/registry-update`,
    token,
    {},
  );
}

export async function listMcpServers(
  token: string,
  base: string = "",
): Promise<McpServerSummary[]> {
  const res = await request<{ servers: McpServerSummary[] }>(
    `${base}/api/v1/mcp/servers`,
    token,
  );
  return res.servers;
}

export async function getMcpServer(
  token: string,
  name: string,
  base: string = "",
): Promise<McpServerDetail> {
  return request<McpServerDetail>(`${base}${mcpPath(name)}`, token);
}

export async function addMcpServer(
  token: string,
  name: string,
  config: McpServerConfig,
  base: string = "",
): Promise<McpServerDetail> {
  return post<McpServerDetail>(`${base}/api/v1/mcp/servers`, token, {
    name,
    config,
  });
}

export async function updateMcpServer(
  token: string,
  name: string,
  config: McpServerConfig,
  base: string = "",
): Promise<McpServerDetail> {
  return patch<McpServerDetail>(`${base}${mcpPath(name)}`, token, {
    name,
    config,
  });
}

export async function removeMcpServer(
  token: string,
  name: string,
  base: string = "",
): Promise<void> {
  await del<{ ok: boolean }>(`${base}${mcpPath(name)}`, token, {});
}

export async function enableMcpServer(
  token: string,
  name: string,
  base: string = "",
): Promise<McpServerDetail> {
  return post<McpServerDetail>(`${base}${mcpPath(name, "/enable")}`, token, {});
}

export async function disableMcpServer(
  token: string,
  name: string,
  base: string = "",
): Promise<McpServerDetail> {
  return post<McpServerDetail>(`${base}${mcpPath(name, "/disable")}`, token, {});
}

export async function reconnectMcpServer(
  token: string,
  name: string,
  base: string = "",
): Promise<McpServerDetail> {
  return post<McpServerDetail>(`${base}${mcpPath(name, "/reconnect")}`, token, {});
}

export async function mcpOauthLogin(
  token: string,
  name: string,
  base: string = "",
): Promise<McpOauthLoginResult> {
  return post<McpOauthLoginResult>(
    `${base}${mcpPath(name, "/oauth/login")}`,
    token,
    {},
  );
}

export async function mcpOauthLogout(
  token: string,
  name: string,
  base: string = "",
): Promise<void> {
  await post<{ ok: boolean }>(`${base}${mcpPath(name, "/oauth/logout")}`, token, {});
}

// -- Telegram channel -------------------------------------------------------

export interface TelegramTestResult { ok: boolean; username: string | null; id: number | null; error: string | null; }
export interface PendingPairing { code: string; channel: string; sender_id: string; created_at: number; expires_at: number; }
export interface TelegramPairing { pending: PendingPairing[]; approved: string[]; }

export async function testTelegramToken(token: string, value: string, base = ""): Promise<TelegramTestResult> {
  return post<TelegramTestResult>(`${base}/api/v1/channels/telegram/test`, token, { token: value });
}
export async function getTelegramPairing(token: string, base = ""): Promise<TelegramPairing> {
  return request<TelegramPairing>(`${base}/api/v1/channels/telegram/pairing`, token);
}
export async function approveTelegramPairing(token: string, code: string, base = ""): Promise<{ ok: boolean }> {
  return post(`${base}/api/v1/channels/telegram/pairing/approve`, token, { code });
}
export async function denyTelegramPairing(token: string, code: string, base = ""): Promise<{ ok: boolean }> {
  return post(`${base}/api/v1/channels/telegram/pairing/deny`, token, { code });
}
export async function revokeTelegramPairing(token: string, senderId: string, base = ""): Promise<{ ok: boolean }> {
  return post(`${base}/api/v1/channels/telegram/pairing/revoke`, token, { sender_id: senderId });
}

export async function startChannel(token: string, name: string, base = ""): Promise<{ ok: boolean; error?: string | null }> {
  return post(`${base}/api/v1/channels/start`, token, { name });
}

export async function stopChannel(token: string, name: string, base = ""): Promise<{ ok: boolean; error?: string | null }> {
  return post(`${base}/api/v1/channels/stop`, token, { name });
}
