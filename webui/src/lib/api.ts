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
  base: string = "",
): Promise<ModelCatalog> {
  const qs = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  return request<ModelCatalog>(`${base}/api/models${qs}`, token);
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
  page: {
    type: string;
    name: string;
    aliases: string[];
    identifiers: Record<string, string[] | string> | null;
    extra: Record<string, unknown>;
    body: string;
    dream_processed_through: string | null;
  };
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
    absorbed_at: string | null;
    absorbed_reason: string | null;
    absorbed_into: string | null;
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
