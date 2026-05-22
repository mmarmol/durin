import type {
  ChatSummary,
  ProviderSettingsUpdate,
  ConfigSnapshot,
  SecretEntry,
  SecretSetInput,
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

export async function setSecret(
  token: string,
  input: SecretSetInput,
  base: string = "",
): Promise<void> {
  const query = new URLSearchParams();
  query.set("name", input.name);
  query.set("service", input.service);
  if (input.account !== undefined) query.set("account", input.account);
  if (input.description !== undefined) query.set("description", input.description);
  if (input.scope !== undefined) query.set("scope", input.scope.join(","));
  if (input.value !== undefined && input.value !== "") query.set("value", input.value);
  await request<{ ok: boolean }>(`${base}/api/secrets/set?${query}`, token);
}

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
