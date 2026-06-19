import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  describeMcpRegistryServer,
  installMcpFromRegistry,
  mcpRegistryRuntime,
  searchMcpRegistry,
} from "@/lib/api";
import type {
  McpRegistryEnvVar,
  McpRegistryHit,
  McpRegistryServerDetail,
  McpRuntimeStatus,
} from "@/lib/types";

function requiredEnv(
  detail: McpRegistryServerDetail,
  prefer: "remote" | "local",
): McpRegistryEnvVar[] {
  if (prefer === "remote" && detail.remotes.length > 0) {
    return detail.remotes[0].headers.filter((e) => e.is_required || e.is_secret);
  }
  if (detail.packages.length > 0) {
    return detail.packages[0].env.filter((e) => e.is_required || e.is_secret);
  }
  return [];
}

function defaultPrefer(detail: McpRegistryServerDetail): "remote" | "local" {
  return detail.remotes.length > 0 ? "remote" : "local";
}

// ---------------------------------------------------------------------------
// Small display helpers
// ---------------------------------------------------------------------------

function OwnerAvatar({ src, login }: { src?: string; login?: string }) {
  const initial = login ? login[0].toUpperCase() : "?";
  return (
    <span className="relative flex size-9 shrink-0 items-center justify-center rounded-full bg-muted text-[13px] font-medium text-muted-foreground">
      {initial}
      {src ? (
        <img
          src={src}
          alt={login ?? "owner"}
          className="absolute inset-0 size-9 rounded-full object-cover"
          onError={(e) => {
            (e.currentTarget as HTMLImageElement).style.display = "none";
          }}
        />
      ) : null}
    </span>
  );
}

function OfficialBadge({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-0.5 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
      <svg
        className="size-2.5"
        viewBox="0 0 10 10"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden
      >
        <path
          d="M2 5l2 2 4-4"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      {label}
    </span>
  );
}

function KindChip({ kind, tx }: { kind: string; tx: (k: string) => string }) {
  const label =
    kind === "remote"
      ? tx("badgeRemote")
      : kind === "both"
        ? tx("badgeBoth")
        : tx("badgeLocal");
  return (
    <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-muted-foreground">
      {label}
    </span>
  );
}

function TopicChips({ topics }: { topics: string[] }) {
  if (!topics.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {topics.slice(0, 6).map((t) => (
        <span
          key={t}
          className="rounded-full bg-muted/60 px-2 py-0.5 text-[10px] text-muted-foreground"
        >
          {t}
        </span>
      ))}
    </div>
  );
}

function ExternalLinkIcon() {
  return (
    <svg
      className="ml-0.5 inline size-2.5 opacity-60"
      viewBox="0 0 10 10"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden
    >
      <path
        d="M4 2H2a1 1 0 00-1 1v5a1 1 0 001 1h5a1 1 0 001-1V6M6 1h3m0 0v3m0-3L4.5 5.5"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

/**
 * MCP discovery pane: search the registry, preview a server, and add it with one
 * click. Remote servers connect (then a separate OAuth login if needed); local
 * servers install. Secret inputs are masked and stored server-side as references.
 */
export function McpDiscoverPane({
  token,
  onClose,
}: {
  token: string;
  onClose: (installed?: boolean) => void;
}) {
  const { t } = useTranslation();
  const tx = (k: string, opts?: Record<string, unknown>) =>
    t(`settings.mcp.discover.${k}`, opts);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<McpRegistryHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [includeAll, setIncludeAll] = useState(false);
  const [selectedHit, setSelectedHit] = useState<McpRegistryHit | null>(null);
  const [detail, setDetail] = useState<McpRegistryServerDetail | null>(null);
  const [prefer, setPrefer] = useState<"remote" | "local">("remote");
  const [envValues, setEnvValues] = useState<Record<string, string>>({});
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runtime, setRuntime] = useState<McpRuntimeStatus | null>(null);

  // When a server is selected (or the local/remote choice changes), check whether
  // the host has the runtime needed to launch it — so we can warn before install.
  useEffect(() => {
    if (!detail) {
      setRuntime(null);
      return;
    }
    let cancelled = false;
    setRuntime(null);
    mcpRegistryRuntime(token, detail.ref, prefer)
      .then((r) => {
        if (!cancelled) setRuntime(r);
      })
      .catch(() => {
        if (!cancelled) setRuntime(null);
      });
    return () => {
      cancelled = true;
    };
  }, [detail, prefer, token]);

  async function runSearch(all = includeAll) {
    if (!query.trim()) return;
    setSearching(true);
    setError(null);
    setDetail(null);
    setSelectedHit(null);
    try {
      setHits(await searchMcpRegistry(token, query.trim(), 10, "", all));
      setSearched(true);
    } catch {
      setError(tx("searchFailed"));
    } finally {
      setSearching(false);
    }
  }

  async function toggleIncludeAll() {
    const next = !includeAll;
    setIncludeAll(next);
    if (searched) await runSearch(next);
  }

  async function openDetail(hit: McpRegistryHit) {
    setError(null);
    setEnvValues({});
    setSelectedHit(hit);
    try {
      const d = await describeMcpRegistryServer(token, hit.ref);
      setDetail(d);
      setPrefer(defaultPrefer(d));
    } catch {
      setError(tx("detailFailed"));
    }
  }

  function goBack() {
    setDetail(null);
    setSelectedHit(null);
  }

  async function doInstall() {
    if (!detail) return;
    setInstalling(true);
    setError(null);
    try {
      await installMcpFromRegistry(token, detail.ref, prefer, envValues);
      onClose(true);
    } catch {
      setError(tx("installFailed"));
      setInstalling(false);
    }
  }

  if (detail) {
    const envs = requiredEnv(detail, prefer);
    const hasLocal = detail.packages.length > 0;
    const hasRemote = detail.remotes.length > 0;
    const missingRequired = envs.some(
      (e) => (e.is_required || e.is_secret) && !(envValues[e.name] ?? "").trim(),
    );

    // Enrich from the hit that triggered the detail view
    const stars = selectedHit?.signals.stars as number | undefined;
    const ownerLogin = selectedHit?.signals.owner_login as string | undefined;
    const ownerUrl = selectedHit?.signals.owner_url as string | undefined;
    const ownerAvatar = selectedHit?.signals.owner_avatar as string | undefined;
    const language = selectedHit?.signals.language as string | undefined;
    const license = selectedHit?.signals.license as string | undefined;
    const isOfficial = selectedHit?.signals.official as boolean | undefined;
    const topics = (selectedHit?.signals.topics as string[] | undefined) ?? [];
    const repoUrl =
      detail.repository || (selectedHit?.signals.repo_url as string | undefined);

    return (
      <div className="space-y-4">
        <button
          type="button"
          className="text-[12px] text-muted-foreground hover:text-foreground"
          onClick={goBack}
        >
          {tx("back")}
        </button>

        {/* Header: avatar + name + official + version */}
        <div className="flex items-start gap-3">
          {(ownerAvatar ?? ownerLogin) ? (
            <OwnerAvatar src={ownerAvatar} login={ownerLogin} />
          ) : null}
          <div className="min-w-0 flex-1 space-y-0.5">
            <div className="flex flex-wrap items-center gap-1.5">
              <h3 className="text-[15px] font-medium text-foreground">{detail.name}</h3>
              {isOfficial ? <OfficialBadge label={tx("official")} /> : null}
              {detail.version ? (
                <span className="text-[11px] text-muted-foreground">v{detail.version}</span>
              ) : null}
            </div>

            {/* Metadata line */}
            <p className="flex flex-wrap items-center gap-x-2 gap-y-0 text-[11px] text-muted-foreground">
              {ownerLogin ? (
                ownerUrl ? (
                  <a
                    href={ownerUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="hover:text-foreground"
                    onClick={(e) => e.stopPropagation()}
                  >
                    {tx("by")} @{ownerLogin}
                    <ExternalLinkIcon />
                  </a>
                ) : (
                  <span>
                    {tx("by")} @{ownerLogin}
                  </span>
                )
              ) : null}
              {stars !== undefined ? (
                <span>★ {stars.toLocaleString()}</span>
              ) : null}
              {language ? <span>{language}</span> : null}
              {license ? <span>{license}</span> : null}
            </p>
          </div>
        </div>

        {/* Description */}
        <p className="text-[13px] leading-5 text-muted-foreground">
          {detail.description || tx("noDescription")}
        </p>

        {/* Topic chips */}
        <TopicChips topics={topics} />

        {/* View on GitHub link */}
        {repoUrl ? (
          <a
            href={repoUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[12px] text-primary underline"
          >
            {tx("viewOnGitHub")}
            <ExternalLinkIcon />
          </a>
        ) : null}

        {/* Remote / local prefer toggle */}
        {hasLocal && hasRemote ? (
          <div className="flex gap-2">
            {(["remote", "local"] as const).map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setPrefer(p)}
                className={
                  "rounded-full border px-3 py-1 text-[12px] " +
                  (prefer === p
                    ? "border-primary bg-primary/10 text-foreground"
                    : "border-border text-muted-foreground")
                }
              >
                {p === "remote" ? tx("preferRemote") : tx("preferLocal")}
              </button>
            ))}
          </div>
        ) : (
          <span className="inline-block rounded-full border border-border px-3 py-1 text-[12px] text-muted-foreground">
            {hasRemote ? tx("hostedOnly") : tx("localOnly")}
          </span>
        )}

        {/* Missing-runtime warning (local model only) */}
        {runtime && runtime.kind === "local" && !runtime.present ? (
          <div className="space-y-1 rounded-[10px] border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[12px] text-foreground">
            {runtime.runtime === "docker" ? (
              <>
                <p>{tx("runtimeMissingDocker")}</p>
                <a
                  href="https://docs.docker.com/get-docker/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-primary underline"
                >
                  {tx("getDocker")}
                  <ExternalLinkIcon />
                </a>
              </>
            ) : (
              <>
                <p>{tx("runtimeMissing", { runtime: runtime.runtime })}</p>
                {runtime.install_command ? (
                  <code className="block select-all rounded bg-muted px-2 py-1 text-[11px] text-foreground">
                    {runtime.install_command}
                  </code>
                ) : null}
              </>
            )}
          </div>
        ) : null}

        {/* Env inputs */}
        {envs.length > 0 ? (
          <div className="space-y-2">
            <p className="text-[12px] font-medium text-foreground">
              {tx("configuration")}
            </p>
            {envs.map((e) => (
              <div key={e.name} className="space-y-1">
                <label className="block text-[12px] text-muted-foreground">
                  {e.name}
                  {e.is_required ? " *" : ""}
                  {e.description ? ` — ${e.description}` : ""}
                </label>
                <Input
                  type={e.is_secret ? "password" : "text"}
                  value={envValues[e.name] ?? ""}
                  onChange={(ev) =>
                    setEnvValues((v) => ({ ...v, [e.name]: ev.target.value }))
                  }
                />
              </div>
            ))}
          </div>
        ) : null}

        {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

        {missingRequired ? (
          <p className="text-[12px] text-muted-foreground">{tx("fillRequired")}</p>
        ) : null}

        <div className="flex gap-2">
          <Button
            size="sm"
            onClick={() => void doInstall()}
            disabled={installing || missingRequired}
          >
            {installing
              ? tx("adding")
              : prefer === "remote"
                ? tx("connect")
                : tx("install")}
          </Button>
          <Button size="sm" variant="outline" onClick={() => onClose()}>
            {tx("cancel")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Input
          autoFocus
          placeholder={tx("searchPlaceholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void runSearch();
          }}
        />
        <Button size="sm" onClick={() => void runSearch()} disabled={searching}>
          {searching ? "…" : tx("search")}
        </Button>
        <Button size="sm" variant="outline" onClick={() => onClose()}>
          {tx("close")}
        </Button>
      </div>

      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

      {searched ? (
        <div className="flex items-center justify-between px-1">
          <span className="text-[11px] text-muted-foreground">
            {includeAll ? tx("showingAll") : tx("showingOfficial")}
          </span>
          <button
            type="button"
            className="text-[11px] text-primary underline"
            onClick={() => void toggleIncludeAll()}
            disabled={searching}
          >
            {includeAll ? tx("officialOnly") : tx("showAll")}
          </button>
        </div>
      ) : null}

      <div className="space-y-1">
        {hits.map((h) => {
          const stars = h.signals.stars as number | undefined;
          const ownerLogin = h.signals.owner_login as string | undefined;
          const ownerUrl = h.signals.owner_url as string | undefined;
          const ownerAvatar = h.signals.owner_avatar as string | undefined;
          const language = h.signals.language as string | undefined;
          const isOfficial = h.signals.official as boolean | undefined;
          const topics = (h.signals.topics as string[] | undefined) ?? [];

          return (
            <button
              key={h.ref}
              type="button"
              onClick={() => void openDetail(h)}
              className="flex w-full items-start gap-3 rounded-[14px] border border-border px-4 py-3 text-left hover:bg-muted/40"
            >
              {/* Owner avatar */}
              <div className="mt-0.5">
                <OwnerAvatar src={ownerAvatar} login={ownerLogin} />
              </div>

              {/* Main content */}
              <div className="min-w-0 flex-1 space-y-1">
                {/* Line 1: name + official badge */}
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[13px] font-medium text-foreground">{h.name}</span>
                  {isOfficial ? <OfficialBadge label={tx("official")} /> : null}
                </div>

                {/* Line 2: @owner · language · kind chip */}
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0 text-[11px] text-muted-foreground">
                  {ownerLogin ? (
                    ownerUrl ? (
                      <a
                        href={ownerUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="hover:text-foreground"
                        onClick={(e) => e.stopPropagation()}
                      >
                        @{ownerLogin}
                        <ExternalLinkIcon />
                      </a>
                    ) : (
                      <span>@{ownerLogin}</span>
                    )
                  ) : null}
                  {language ? <span>{language}</span> : null}
                  <KindChip kind={h.kind} tx={tx} />
                </div>

                {/* Description */}
                {h.description ? (
                  <p className="truncate text-[12px] leading-5 text-muted-foreground">
                    {h.description}
                  </p>
                ) : null}

                {/* Topic chips */}
                <TopicChips topics={topics} />
              </div>

              {/* Stars — right side */}
              {stars !== undefined ? (
                <span className="mt-0.5 shrink-0 text-[11px] text-muted-foreground">
                  ★ {stars.toLocaleString()}
                </span>
              ) : null}
            </button>
          );
        })}
        {searched && !searching && hits.length === 0 ? (
          <p className="px-1 text-[12px] text-muted-foreground">{tx("noResults")}</p>
        ) : null}
      </div>
    </div>
  );
}
