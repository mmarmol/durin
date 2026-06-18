import { useCallback, useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  KeyRound,
  Loader2,
  LogOut,
  Pencil,
  Plus,
  RotateCw,
  Search,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "@/components/settings/primitives";
import { McpDiscoverPane } from "@/components/settings/McpDiscoverPane";
import {
  ApiError,
  addMcpServer,
  disableMcpServer,
  enableMcpServer,
  getMcpServer,
  listMcpServers,
  listMcpUpdates,
  mcpOauthLogin,
  mcpOauthLogout,
  reconnectMcpServer,
  removeMcpServer,
  updateMcpFromRegistry,
  updateMcpServer,
} from "@/lib/api";
import type {
  McpOauthStaticConfig,
  McpServerConfig,
  McpServerDetail,
  McpServerSummary,
  McpStatus,
} from "@/lib/types";
import { cn } from "@/lib/utils";

// --- Form state -------------------------------------------------------------
// A flat, string-backed mirror of McpServerConfig. Multi-value fields (args,
// env, headers, enabled_tools) are edited as text and parsed on submit; the
// original oauth object (if any) is stashed so toggling the checkbox off/on
// preserves a configured static client rather than flattening it to `true`.

type TransportChoice = "auto" | "stdio" | "sse" | "streamableHttp";

interface McpFormState {
  name: string;
  type: TransportChoice;
  command: string;
  args: string;
  env: string;
  url: string;
  headers: string;
  enabled: boolean;
  toolTimeout: string;
  enabledTools: string;
  oauth: boolean;
  oauthClientId: string;
  oauthClientSecret: string;
  allowPrivateUrl: boolean;
  spawnEgressPolicy: "warn" | "refuse" | "off";
  malwareCheck: boolean;
}

const EMPTY_MCP_FORM: McpFormState = {
  name: "",
  type: "auto",
  command: "",
  args: "",
  env: "",
  url: "",
  headers: "",
  enabled: true,
  toolTimeout: "",
  enabledTools: "*",
  oauth: false,
  oauthClientId: "",
  oauthClientSecret: "",
  allowPrivateUrl: false,
  spawnEgressPolicy: "warn",
  malwareCheck: false,
};

// Split a textarea into trimmed, non-empty lines (commas also separate, so a
// single comma-delimited line works too).
function splitList(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

// Parse `KEY=value` lines into a record. Lines without `=` are skipped.
function parseKeyValues(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    if (eq <= 0) continue;
    out[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
  }
  return out;
}

function formatKeyValues(record: Record<string, string> | undefined): string {
  if (!record) return "";
  return Object.entries(record)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

// Status dot colors, mirroring the success/warning/critical/muted tokens used
// across Skills/Secrets (emerald/amber/destructive/muted-foreground).
function statusDotClass(status: McpStatus): string {
  switch (status) {
    case "connected":
      return "bg-emerald-500";
    case "connecting":
      return "bg-amber-500";
    case "failed":
      return "bg-destructive";
    case "needs_auth":
      return "bg-amber-500";
    case "disabled":
    default:
      return "bg-muted-foreground/40";
  }
}

export function McpSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [expandedName, setExpandedName] = useState<string | null>(null);
  const [detail, setDetail] = useState<McpServerDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [form, setForm] = useState<McpFormState>(EMPTY_MCP_FORM);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [updates, setUpdates] = useState<Record<string, string>>({});
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // Carries the existing static-OAuth client object so a checkbox toggle does
  // not lose it; null means "no object configured".
  const [oauthObject, setOauthObject] =
    useState<McpOauthStaticConfig | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const formAnchorRef = useRef<HTMLDivElement | null>(null);

  // Map an ApiError status to a friendly banner message.
  const describeError = useCallback(
    (e: unknown): string => {
      if (e instanceof ApiError) {
        if (e.status === 409) return t("settings.mcp.errorConflict");
        if (e.status === 422) return t("settings.mcp.errorInvalid");
        if (e.status === 404) return t("settings.mcp.errorNotFound");
      }
      return t("settings.mcp.errorGeneric");
    },
    [t],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setServers(await listMcpServers(token));
      void listMcpUpdates(token)
        .then((ups) =>
          setUpdates(Object.fromEntries(ups.map((u) => [u.name, u.latest]))),
        )
        .catch(() => {});
    } catch (e) {
      setError(describeError(e));
    } finally {
      setLoading(false);
    }
  }, [token, describeError]);

  useEffect(() => {
    void load();
  }, [load]);

  const onUpdate = useCallback(
    async (name: string) => {
      setBusy(true);
      setError(null);
      try {
        await updateMcpFromRegistry(token, name);
        await load();
      } catch (e) {
        setError(describeError(e));
      } finally {
        setBusy(false);
      }
    },
    [token, load, describeError],
  );

  // Re-fetch the detail for the currently-open server (used after a mutation
  // that should leave the detail pane open, e.g. enable/disable or OAuth).
  const refreshDetail = useCallback(
    async (name: string) => {
      try {
        setDetail(await getMcpServer(token, name));
      } catch (e) {
        setError(describeError(e));
      }
    },
    [token, describeError],
  );

  const openDetail = useCallback(
    async (name: string) => {
      if (expandedName === name) {
        setExpandedName(null);
        setDetail(null);
        return;
      }
      setExpandedName(name);
      setDetail(null);
      setDetailLoading(true);
      try {
        setDetail(await getMcpServer(token, name));
      } catch (e) {
        setError(describeError(e));
      } finally {
        setDetailLoading(false);
      }
    },
    [token, expandedName, describeError],
  );

  const resetForm = useCallback(() => {
    setForm(EMPTY_MCP_FORM);
    setEditingName(null);
    setAdding(false);
    setAdvancedOpen(false);
    setOauthObject(null);
  }, []);

  const scrollToForm = useCallback(() => {
    setTimeout(() => {
      formAnchorRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }, 0);
  }, []);

  const onAdd = useCallback(() => {
    setForm(EMPTY_MCP_FORM);
    setEditingName(null);
    setAdding(true);
    setAdvancedOpen(false);
    setOauthObject(null);
    scrollToForm();
  }, [scrollToForm]);

  const onEdit = useCallback(
    (d: McpServerDetail) => {
      const c = d.config;
      const oauthIsObject =
        c.oauth !== null && c.oauth !== undefined && typeof c.oauth === "object";
      setOauthObject(oauthIsObject ? (c.oauth as McpOauthStaticConfig) : null);
      setForm({
        name: d.name,
        type: (c.type ?? "auto") as TransportChoice,
        command: c.command ?? "",
        args: (c.args ?? []).join("\n"),
        env: formatKeyValues(c.env),
        url: c.url ?? "",
        headers: formatKeyValues(c.headers),
        enabled: c.enabled ?? d.enabled,
        toolTimeout: c.tool_timeout != null ? String(c.tool_timeout) : "",
        enabledTools: (c.enabled_tools ?? ["*"]).join(", "),
        oauth: oauthIsObject ? true : Boolean(c.oauth),
        oauthClientId: oauthIsObject
          ? ((c.oauth as McpOauthStaticConfig).client_id ?? "")
          : "",
        oauthClientSecret: oauthIsObject
          ? ((c.oauth as McpOauthStaticConfig).client_secret ?? "")
          : "",
        allowPrivateUrl: Boolean(c.allow_private_url),
        spawnEgressPolicy: c.spawn_egress_policy ?? "warn",
        malwareCheck: Boolean(c.malware_check),
      });
      setEditingName(d.name);
      setAdding(false);
      setAdvancedOpen(false);
      scrollToForm();
    },
    [scrollToForm],
  );

  // Build the snake_case McpServerConfig payload, omitting empty optional
  // fields so the backend's own defaults apply.
  const buildConfig = useCallback((): McpServerConfig => {
    const config: McpServerConfig = { enabled: form.enabled };

    if (form.type !== "auto") config.type = form.type;

    const isHttp = form.type === "sse" || form.type === "streamableHttp";
    if (!isHttp) {
      if (form.command.trim()) config.command = form.command.trim();
      const args = splitList(form.args);
      if (args.length) config.args = args;
      const env = parseKeyValues(form.env);
      if (Object.keys(env).length) config.env = env;
    }
    if (form.type === "auto" || isHttp) {
      if (form.url.trim()) config.url = form.url.trim();
      const headers = parseKeyValues(form.headers);
      if (Object.keys(headers).length) config.headers = headers;
    }

    if (form.toolTimeout.trim()) {
      const n = Number(form.toolTimeout);
      if (!Number.isNaN(n)) config.tool_timeout = n;
    }
    const enabledTools = splitList(form.enabledTools);
    if (enabledTools.length) config.enabled_tools = enabledTools;

    // OAuth: a client id/secret means static registration (for servers without
    // dynamic registration, e.g. GitHub); otherwise `true` uses DCR. Preserve any
    // other fields (scope, callback_port) from an existing object.
    if (form.oauth) {
      const cid = form.oauthClientId.trim();
      const csecret = form.oauthClientSecret.trim();
      if (cid || csecret) {
        config.oauth = {
          ...(oauthObject ?? {}),
          ...(cid ? { client_id: cid } : {}),
          ...(csecret ? { client_secret: csecret } : {}),
        };
      } else {
        config.oauth = oauthObject ?? true;
      }
    } else {
      config.oauth = false;
    }

    if (form.allowPrivateUrl) config.allow_private_url = true;
    if (form.spawnEgressPolicy !== "warn")
      config.spawn_egress_policy = form.spawnEgressPolicy;
    if (form.malwareCheck) config.malware_check = true;

    return config;
  }, [form, oauthObject]);

  const canSave = form.name.trim() !== "" && !busy;

  const onSubmit = useCallback(async () => {
    if (!canSave) return;
    setBusy(true);
    setError(null);
    const name = form.name.trim();
    const config = buildConfig();
    try {
      if (editingName) await updateMcpServer(token, name, config);
      else await addMcpServer(token, name, config);
      resetForm();
      await load();
      if (expandedName === name) await refreshDetail(name);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }, [
    canSave,
    form.name,
    buildConfig,
    editingName,
    token,
    resetForm,
    load,
    expandedName,
    refreshDetail,
    describeError,
  ]);

  const onToggleEnabled = useCallback(
    async (server: McpServerSummary) => {
      setBusy(true);
      setError(null);
      try {
        if (server.enabled) await disableMcpServer(token, server.name);
        else await enableMcpServer(token, server.name);
        await load();
        if (expandedName === server.name) await refreshDetail(server.name);
      } catch (e) {
        setError(describeError(e));
      } finally {
        setBusy(false);
      }
    },
    [token, load, expandedName, refreshDetail, describeError],
  );

  const onDelete = useCallback(
    async (name: string) => {
      setBusy(true);
      setError(null);
      try {
        await removeMcpServer(token, name);
        if (editingName === name) resetForm();
        if (expandedName === name) {
          setExpandedName(null);
          setDetail(null);
        }
        setConfirmDelete(null);
        await load();
      } catch (e) {
        setError(describeError(e));
      } finally {
        setBusy(false);
      }
    },
    [token, editingName, expandedName, resetForm, load, describeError],
  );

  const text =
    (key: keyof McpFormState) =>
    (
      e: React.ChangeEvent<
        HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
      >,
    ) =>
      setForm((prev) => ({ ...prev, [key]: e.target.value }));

  const check =
    (key: keyof McpFormState) => (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: e.target.checked }));

  const isAutoForm = form.type === "auto";
  const isHttpForm =
    form.type === "sse" || form.type === "streamableHttp" || isAutoForm;
  const isStdioForm = form.type === "stdio" || isAutoForm;

  if (discovering) {
    return (
      <McpDiscoverPane
        token={token}
        onClose={(installed) => {
          setDiscovering(false);
          if (installed) void load();
        }}
      />
    );
  }

  return (
    <div className="space-y-5">
      <p className="px-1 text-[13px] leading-5 text-muted-foreground">
        {t("settings.mcp.description")}
      </p>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <section>
        <div className="mb-2 flex items-center justify-between px-1">
          <SettingsSectionTitle>
            {t("settings.mcp.servers")}
          </SettingsSectionTitle>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => setDiscovering(true)}
              className="rounded-full"
            >
              <Search className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              Discover
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={onAdd}
              className="rounded-full"
            >
              <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {t("settings.mcp.add")}
            </Button>
          </div>
        </div>
        <SettingsGroup>
          {loading ? (
            <SettingsRow title={t("settings.status.loading")} />
          ) : servers.length === 0 ? (
            <div className="flex flex-col items-start gap-2 px-4 py-5 sm:px-5">
              <p className="text-[13px] font-medium text-foreground">
                {t("settings.mcp.emptyTitle")}
              </p>
              <p className="text-[12px] leading-5 text-muted-foreground">
                {t("settings.mcp.emptyHint")}
              </p>
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={onAdd}
                className="mt-1 rounded-full"
              >
                <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                {t("settings.mcp.add")}
              </Button>
            </div>
          ) : (
            servers.map((server) => {
              const isExpanded = expandedName === server.name;
              return (
                <div key={server.name}>
                  <SettingsRow
                    title={
                      <button
                        type="button"
                        onClick={() => void openDetail(server.name)}
                        className="flex w-full items-center gap-2 text-left hover:text-foreground/80"
                        aria-expanded={isExpanded}
                        aria-controls={`mcp-detail-${server.name}`}
                      >
                        <ChevronDown
                          className={`h-3.5 w-3.5 text-muted-foreground transition-transform ${
                            isExpanded ? "" : "-rotate-90"
                          }`}
                          aria-hidden
                        />
                        <span
                          className={cn(
                            "h-2 w-2 shrink-0 rounded-full",
                            statusDotClass(server.status),
                          )}
                          aria-label={t(`settings.mcp.status.${server.status}`)}
                        />
                        <span>{server.name}</span>
                        {updates[server.name] ? (
                          <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-600">
                            update → {updates[server.name]}
                          </span>
                        ) : null}
                      </button>
                    }
                    description={[
                      server.transport,
                      server.target ? `· ${server.target}` : "",
                      `· ${t("settings.mcp.toolCount", {
                        count: server.tool_count,
                      })}`,
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    <div className="flex items-center gap-2">
                      {updates[server.name] ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => void onUpdate(server.name)}
                          className="rounded-full"
                        >
                          Update to {updates[server.name]}
                        </Button>
                      ) : null}
                      <label className="flex cursor-pointer items-center gap-1.5 text-[12px] text-muted-foreground">
                        <input
                          type="checkbox"
                          checked={server.enabled}
                          disabled={busy}
                          onChange={() => void onToggleEnabled(server)}
                          className="h-3.5 w-3.5 rounded border-input accent-primary"
                        />
                        {server.enabled
                          ? t("settings.mcp.enabled")
                          : t("settings.mcp.disabled")}
                      </label>
                    </div>
                  </SettingsRow>
                  {isExpanded ? (
                    <div
                      id={`mcp-detail-${server.name}`}
                      className="space-y-3 bg-muted/30 px-5 py-3 text-[12px] leading-5"
                    >
                      {detailLoading || !detail ? (
                        <div className="flex items-center gap-2 text-muted-foreground">
                          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                          {t("settings.status.loading")}
                        </div>
                      ) : (
                        <McpDetailPane
                          detail={detail}
                          token={token}
                          busy={busy}
                          setBusy={setBusy}
                          onError={(e) => setError(describeError(e))}
                          onRefresh={async () => {
                            await load();
                            await refreshDetail(server.name);
                          }}
                          onEdit={() => onEdit(detail)}
                          confirmingDelete={confirmDelete === server.name}
                          onAskDelete={() => setConfirmDelete(server.name)}
                          onCancelDelete={() => setConfirmDelete(null)}
                          onConfirmDelete={() => void onDelete(server.name)}
                        />
                      )}
                    </div>
                  ) : null}
                </div>
              );
            })
          )}
        </SettingsGroup>
      </section>

      {adding || editingName ? (
        <section ref={formAnchorRef}>
          <SettingsSectionTitle>
            {editingName
              ? t("settings.mcp.editTitle", { name: editingName })
              : t("settings.mcp.addTitle")}
          </SettingsSectionTitle>
          <SettingsGroup>
            <SettingsRow
              title={t("settings.mcp.fieldName")}
              description={t("settings.mcp.hintName")}
            >
              <Input
                value={form.name}
                onChange={text("name")}
                placeholder="everything"
                className="w-[260px]"
                readOnly={editingName !== null}
                disabled={editingName !== null}
              />
            </SettingsRow>

            <SettingsRow
              title={t("settings.mcp.fieldType")}
              description={t("settings.mcp.hintType")}
            >
              <select
                value={form.type}
                onChange={text("type")}
                className="flex h-10 w-[260px] rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <option value="auto">{t("settings.mcp.typeAuto")}</option>
                <option value="stdio">stdio</option>
                <option value="sse">sse</option>
                <option value="streamableHttp">streamableHttp</option>
              </select>
            </SettingsRow>

            {isAutoForm ? (
              <div className="px-4 pt-2 text-[12px] leading-5 text-muted-foreground sm:px-5">
                {t("settings.mcp.autoHint")}
              </div>
            ) : null}

            {isStdioForm ? (
              <>
                {isAutoForm ? (
                  <div className="px-4 pt-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground sm:px-5">
                    {t("settings.mcp.stdioGroup")}
                  </div>
                ) : null}
                <SettingsRow
                  title={t("settings.mcp.fieldCommand")}
                  description={t("settings.mcp.hintCommand")}
                >
                  <Input
                    value={form.command}
                    onChange={text("command")}
                    placeholder="npx"
                    className="w-[260px]"
                  />
                </SettingsRow>
                <SettingsRow
                  title={t("settings.mcp.fieldArgs")}
                  description={t("settings.mcp.hintArgs")}
                >
                  <Textarea
                    value={form.args}
                    onChange={text("args")}
                    placeholder={"-y\n@modelcontextprotocol/server-everything"}
                    className="w-[260px] font-mono text-[12px]"
                  />
                </SettingsRow>
                <SettingsRow
                  title={t("settings.mcp.fieldEnv")}
                  description={t("settings.mcp.hintEnv")}
                >
                  <Textarea
                    value={form.env}
                    onChange={text("env")}
                    placeholder={"API_KEY=..."}
                    className="w-[260px] font-mono text-[12px]"
                  />
                </SettingsRow>
              </>
            ) : null}

            {isHttpForm ? (
              <>
                {isAutoForm ? (
                  <div className="px-4 pt-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground sm:px-5">
                    {t("settings.mcp.httpGroup")}
                  </div>
                ) : null}
                <SettingsRow
                  title={t("settings.mcp.fieldUrl")}
                  description={t("settings.mcp.hintUrl")}
                >
                  <Input
                    value={form.url}
                    onChange={text("url")}
                    placeholder="https://example.com/mcp"
                    className="w-[260px]"
                  />
                </SettingsRow>
                <SettingsRow
                  title={t("settings.mcp.fieldHeaders")}
                  description={t("settings.mcp.hintHeaders")}
                >
                  <Textarea
                    value={form.headers}
                    onChange={text("headers")}
                    placeholder={"Authorization=Bearer ..."}
                    className="w-[260px] font-mono text-[12px]"
                  />
                </SettingsRow>
              </>
            ) : null}

            <SettingsRow title={t("settings.mcp.fieldEnabled")}>
              <label className="flex cursor-pointer items-center gap-2 text-[13px] text-foreground">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={check("enabled")}
                  className="h-4 w-4 rounded border-input accent-primary"
                />
                {t("settings.mcp.fieldEnabled")}
              </label>
            </SettingsRow>

            <div className="px-4 py-2 sm:px-5">
              <button
                type="button"
                onClick={() => setAdvancedOpen((v) => !v)}
                className="flex items-center gap-1.5 text-[12px] font-medium text-muted-foreground hover:text-foreground"
                aria-expanded={advancedOpen}
              >
                <ChevronDown
                  className={`h-3.5 w-3.5 transition-transform ${
                    advancedOpen ? "" : "-rotate-90"
                  }`}
                  aria-hidden
                />
                {t("settings.mcp.advanced")}
              </button>
            </div>

            {advancedOpen ? (
              <>
                <SettingsRow
                  title={t("settings.mcp.fieldToolTimeout")}
                  description={t("settings.mcp.hintToolTimeout")}
                >
                  <Input
                    type="number"
                    value={form.toolTimeout}
                    onChange={text("toolTimeout")}
                    placeholder="30"
                    className="w-[260px]"
                  />
                </SettingsRow>
                <SettingsRow
                  title={t("settings.mcp.fieldEnabledTools")}
                  description={t("settings.mcp.hintEnabledTools")}
                >
                  <Input
                    value={form.enabledTools}
                    onChange={text("enabledTools")}
                    placeholder="*"
                    className="w-[260px]"
                  />
                </SettingsRow>
                <SettingsRow
                  title={t("settings.mcp.fieldOauth")}
                  description={t("settings.mcp.hintOauth")}
                >
                  <label className="flex cursor-pointer items-center gap-2 text-[13px] text-foreground">
                    <input
                      type="checkbox"
                      checked={form.oauth}
                      onChange={check("oauth")}
                      className="h-4 w-4 rounded border-input accent-primary"
                    />
                    {t("settings.mcp.fieldOauth")}
                  </label>
                </SettingsRow>
                {form.oauth ? (
                  <>
                    <SettingsRow
                      title={t("settings.mcp.fieldOauthClientId")}
                      description={t("settings.mcp.hintOauthClientId")}
                    >
                      <Input
                        value={form.oauthClientId}
                        onChange={text("oauthClientId")}
                        className="w-[260px]"
                        autoComplete="off"
                      />
                    </SettingsRow>
                    <SettingsRow
                      title={t("settings.mcp.fieldOauthClientSecret")}
                      description={t("settings.mcp.hintOauthClientSecret")}
                    >
                      <Input
                        type="password"
                        value={form.oauthClientSecret}
                        onChange={text("oauthClientSecret")}
                        className="w-[260px]"
                        autoComplete="off"
                      />
                    </SettingsRow>
                  </>
                ) : null}
                <SettingsRow
                  title={t("settings.mcp.fieldAllowPrivateUrl")}
                  description={t("settings.mcp.hintAllowPrivateUrl")}
                >
                  <label className="flex cursor-pointer items-center gap-2 text-[13px] text-foreground">
                    <input
                      type="checkbox"
                      checked={form.allowPrivateUrl}
                      onChange={check("allowPrivateUrl")}
                      className="h-4 w-4 rounded border-input accent-primary"
                    />
                    {t("settings.mcp.fieldAllowPrivateUrl")}
                  </label>
                </SettingsRow>
                <SettingsRow
                  title={t("settings.mcp.fieldSpawnEgress")}
                  description={t("settings.mcp.hintSpawnEgress")}
                >
                  <select
                    value={form.spawnEgressPolicy}
                    onChange={text("spawnEgressPolicy")}
                    className="flex h-10 w-[260px] rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  >
                    <option value="warn">{t("settings.mcp.egressWarn")}</option>
                    <option value="refuse">
                      {t("settings.mcp.egressRefuse")}
                    </option>
                    <option value="off">{t("settings.mcp.egressOff")}</option>
                  </select>
                </SettingsRow>
                <SettingsRow
                  title={t("settings.mcp.fieldMalwareCheck")}
                  description={t("settings.mcp.hintMalwareCheck")}
                >
                  <label className="flex cursor-pointer items-center gap-2 text-[13px] text-foreground">
                    <input
                      type="checkbox"
                      checked={form.malwareCheck}
                      onChange={check("malwareCheck")}
                      className="h-4 w-4 rounded border-input accent-primary"
                    />
                    {t("settings.mcp.fieldMalwareCheck")}
                  </label>
                </SettingsRow>
              </>
            ) : null}

            <div className="flex items-center justify-end gap-2 px-4 py-3 sm:px-5">
              <Button
                size="sm"
                variant="ghost"
                onClick={resetForm}
                disabled={busy}
                className="rounded-full"
              >
                {t("settings.mcp.cancel")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => void onSubmit()}
                disabled={!canSave}
                className="rounded-full"
              >
                {editingName ? (
                  <Pencil className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                ) : (
                  <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                )}
                {busy
                  ? t("settings.mcp.saving")
                  : editingName
                    ? t("settings.mcp.saveEdit")
                    : t("settings.mcp.save")}
              </Button>
            </div>
          </SettingsGroup>
        </section>
      ) : null}
    </div>
  );
}

// --- Detail pane ------------------------------------------------------------

function McpDetailPane({
  detail,
  token,
  busy,
  setBusy,
  onError,
  onRefresh,
  onEdit,
  confirmingDelete,
  onAskDelete,
  onCancelDelete,
  onConfirmDelete,
}: {
  detail: McpServerDetail;
  token: string;
  busy: boolean;
  setBusy: (b: boolean) => void;
  onError: (e: unknown) => void;
  onRefresh: () => Promise<void>;
  onEdit: () => void;
  confirmingDelete: boolean;
  onAskDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
}) {
  const { t } = useTranslation();
  const [oauthBusy, setOauthBusy] = useState(false);
  const [reconnectBusy, setReconnectBusy] = useState(false);
  // The popup handle + its watchdog interval, so cleanup can clear both.
  const pollRef = useRef<number | null>(null);
  const messageHandlerRef = useRef<((e: MessageEvent) => void) | null>(null);

  const stopWatching = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (messageHandlerRef.current) {
      window.removeEventListener("message", messageHandlerRef.current);
      messageHandlerRef.current = null;
    }
  }, []);

  useEffect(() => stopWatching, [stopWatching]);

  const onLogin = useCallback(async () => {
    setOauthBusy(true);
    try {
      const result = await mcpOauthLogin(token, detail.name);
      const popup = window.open(
        result.authorization_url,
        "durin-mcp-oauth",
        "width=540,height=720",
      );

      let done = false;
      const finish = async () => {
        if (done) return; // guard: both the message and the poll can fire
        done = true;
        stopWatching();
        setOauthBusy(false);
        // The gateway reconnects the server after the token is stored (race-free
        // server-side; the popup closes a beat before the SDK finishes the token
        // exchange, so the webui must NOT reconnect itself). Refresh a few times
        // so the UI reflects the server coming up (needs_auth -> connecting ->
        // connected) without a manual refresh.
        for (let i = 0; i < 6; i++) {
          await onRefresh();
          await new Promise((r) => setTimeout(r, 1800));
        }
      };

      // Faster completion signal: the callback page posts a message back.
      const handler = (e: MessageEvent) => {
        if (
          e.data &&
          typeof e.data === "object" &&
          (e.data as { type?: string }).type === "durin-mcp-oauth"
        ) {
          void finish();
        }
      };
      messageHandlerRef.current = handler;
      window.addEventListener("message", handler);

      // Fallback: poll the popup handle until it closes (bounded ~5 min).
      const startedAt = Date.now();
      pollRef.current = window.setInterval(() => {
        const closed = !popup || popup.closed;
        const timedOut = Date.now() - startedAt > 5 * 60 * 1000;
        if (closed || timedOut) void finish();
      }, 700);
    } catch (e) {
      setOauthBusy(false);
      onError(e);
    }
  }, [token, detail.name, onRefresh, onError, stopWatching]);

  const onLogout = useCallback(async () => {
    setBusy(true);
    try {
      await mcpOauthLogout(token, detail.name);
      await onRefresh();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }, [token, detail.name, onRefresh, onError, setBusy]);

  const onReconnect = useCallback(async () => {
    setBusy(true);
    setReconnectBusy(true);
    try {
      await reconnectMcpServer(token, detail.name);
      await onRefresh();
    } catch (e) {
      onError(e);
    } finally {
      setReconnectBusy(false);
      setBusy(false);
    }
  }, [token, detail.name, onRefresh, onError, setBusy]);

  const c = detail.config;

  return (
    <div className="space-y-3">
      {/* Config summary */}
      <div className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1.5">
        <span className="text-muted-foreground">
          {t("settings.mcp.fieldTransport")}
        </span>
        <span className="font-mono">{detail.transport || "—"}</span>
        <span className="text-muted-foreground">
          {t("settings.mcp.fieldTarget")}
        </span>
        <span className="break-all font-mono">{detail.target || "—"}</span>
        <span className="text-muted-foreground">
          {t("settings.mcp.fieldStatus")}
        </span>
        <span>{t(`settings.mcp.status.${detail.status}`)}</span>
        {c.tool_timeout != null ? (
          <>
            <span className="text-muted-foreground">
              {t("settings.mcp.fieldToolTimeout")}
            </span>
            <span className="font-mono">{c.tool_timeout}</span>
          </>
        ) : null}
        {c.enabled_tools && c.enabled_tools.length ? (
          <>
            <span className="text-muted-foreground">
              {t("settings.mcp.fieldEnabledTools")}
            </span>
            <span className="font-mono">{c.enabled_tools.join(", ")}</span>
          </>
        ) : null}
      </div>

      {/* Failure detail */}
      {detail.status === "failed" && detail.error ? (
        <div className="rounded-[12px] border border-destructive/20 bg-destructive/5 px-3 py-2 text-destructive">
          {detail.error}
        </div>
      ) : null}

      {/* Tools */}
      <div>
        <div className="mb-1 font-medium text-foreground/80">
          {t("settings.mcp.tools")}
        </div>
        {detail.tools.length === 0 ? (
          <div className="text-muted-foreground">
            {t("settings.mcp.noTools")}
          </div>
        ) : (
          <ul className="space-y-1">
            {detail.tools.map((tool) => (
              <li key={tool.name}>
                <span className="font-mono text-foreground/90">
                  {tool.name}
                </span>
                {tool.description ? (
                  <span className="text-muted-foreground">
                    {" — "}
                    {tool.description}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* OAuth */}
      {detail.oauth_required ? (
        <div className="flex items-center gap-2">
          {detail.oauth_authenticated ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => void onLogout()}
              className="rounded-full"
            >
              <LogOut className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {t("settings.mcp.oauthSignOut")}
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              disabled={oauthBusy}
              onClick={() => void onLogin()}
              className="rounded-full"
            >
              {oauthBusy ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <KeyRound className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              )}
              {t("settings.mcp.oauthAuthenticate")}
            </Button>
          )}
        </div>
      ) : null}

      {/* Reconnect / Edit / Delete */}
      <div className="flex items-center gap-1 pt-1">
        {detail.enabled ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => void onReconnect()}
            className="rounded-full"
          >
            {reconnectBusy ? (
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <RotateCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            )}
            {reconnectBusy
              ? t("settings.mcp.reconnecting")
              : t("settings.mcp.reconnect")}
          </Button>
        ) : null}
        <Button
          size="sm"
          variant="ghost"
          disabled={busy}
          onClick={onEdit}
          className="rounded-full"
        >
          <Pencil className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          {t("settings.mcp.edit")}
        </Button>
        {confirmingDelete ? (
          <div className="flex items-center gap-1.5">
            <span className="text-[12px] text-muted-foreground">
              {t("settings.mcp.confirmDelete")}
            </span>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={onConfirmDelete}
              className="rounded-full text-destructive hover:text-destructive"
            >
              <Trash2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {t("settings.mcp.delete")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={onCancelDelete}
              className="rounded-full"
            >
              {t("settings.mcp.cancel")}
            </Button>
          </div>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={onAskDelete}
            className="rounded-full text-destructive hover:text-destructive"
          >
            <Trash2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {t("settings.mcp.delete")}
          </Button>
        )}
      </div>

      {detail.enabled && detail.status === "failed" ? (
        <p className="text-[11px] leading-4 text-muted-foreground">
          {t("settings.mcp.reconnectHint")}
        </p>
      ) : null}
    </div>
  );
}
