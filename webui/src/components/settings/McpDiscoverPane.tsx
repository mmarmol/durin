import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  describeMcpRegistryServer,
  installMcpFromRegistry,
  searchMcpRegistry,
} from "@/lib/api";
import type {
  McpRegistryEnvVar,
  McpRegistryHit,
  McpRegistryServerDetail,
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
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<McpRegistryHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [detail, setDetail] = useState<McpRegistryServerDetail | null>(null);
  const [prefer, setPrefer] = useState<"remote" | "local">("remote");
  const [envValues, setEnvValues] = useState<Record<string, string>>({});
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function runSearch() {
    if (!query.trim()) return;
    setSearching(true);
    setError(null);
    setDetail(null);
    try {
      setHits(await searchMcpRegistry(token, query.trim()));
      setSearched(true);
    } catch {
      setError("Search failed.");
    } finally {
      setSearching(false);
    }
  }

  async function openDetail(ref: string) {
    setError(null);
    setEnvValues({});
    try {
      const d = await describeMcpRegistryServer(token, ref);
      setDetail(d);
      setPrefer(defaultPrefer(d));
    } catch {
      setError("Could not load server details.");
    }
  }

  async function doInstall() {
    if (!detail) return;
    setInstalling(true);
    setError(null);
    try {
      await installMcpFromRegistry(token, detail.ref, prefer, envValues);
      onClose(true);
    } catch {
      setError("Install failed.");
      setInstalling(false);
    }
  }

  if (detail) {
    const envs = requiredEnv(detail, prefer);
    const hasLocal = detail.packages.length > 0;
    const hasRemote = detail.remotes.length > 0;
    return (
      <div className="space-y-4">
        <button
          type="button"
          className="text-[12px] text-muted-foreground hover:text-foreground"
          onClick={() => setDetail(null)}
        >
          ← Back to results
        </button>
        <div className="space-y-1">
          <h3 className="text-[15px] font-medium text-foreground">{detail.name}</h3>
          {detail.version ? (
            <p className="text-[11px] text-muted-foreground">v{detail.version}</p>
          ) : null}
          <p className="text-[13px] leading-5 text-muted-foreground">
            {detail.description || "No description."}
          </p>
          {detail.repository ? (
            <a
              href={detail.repository}
              target="_blank"
              rel="noreferrer"
              className="break-all text-[12px] text-primary underline"
            >
              {detail.repository}
            </a>
          ) : null}
        </div>

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
                {p === "remote"
                  ? "Hosted (no install)"
                  : "Local (runs on your machine)"}
              </button>
            ))}
          </div>
        ) : (
          <span className="inline-block rounded-full border border-border px-3 py-1 text-[12px] text-muted-foreground">
            {hasRemote ? "Hosted — no install" : "Local — runs on your machine"}
          </span>
        )}

        {envs.length > 0 ? (
          <div className="space-y-2">
            <p className="text-[12px] font-medium text-foreground">Configuration</p>
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

        <div className="flex gap-2">
          <Button size="sm" onClick={() => void doInstall()} disabled={installing}>
            {installing ? "Adding…" : prefer === "remote" ? "Connect" : "Install"}
          </Button>
          <Button size="sm" variant="outline" onClick={() => onClose()}>
            Cancel
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
          placeholder="Search MCP servers (e.g. jira, postgres, github)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void runSearch();
          }}
        />
        <Button size="sm" onClick={() => void runSearch()} disabled={searching}>
          {searching ? "…" : "Search"}
        </Button>
        <Button size="sm" variant="outline" onClick={() => onClose()}>
          Close
        </Button>
      </div>

      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

      <div className="space-y-1">
        {hits.map((h) => (
          <button
            key={h.ref}
            type="button"
            onClick={() => void openDetail(h.ref)}
            className="flex w-full flex-col items-start gap-0.5 rounded-[14px] border border-border px-4 py-3 text-left hover:bg-muted/40"
          >
            <span className="flex items-center gap-2">
              <span className="text-[13px] font-medium text-foreground">
                {h.name}
              </span>
              <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-muted-foreground">
                {h.kind === "remote"
                  ? "no install"
                  : h.kind === "both"
                    ? "hosted / local"
                    : "local"}
              </span>
            </span>
            {h.description ? (
              <span className="text-[12px] leading-5 text-muted-foreground">
                {h.description}
              </span>
            ) : null}
          </button>
        ))}
        {searched && !searching && hits.length === 0 ? (
          <p className="px-1 text-[12px] text-muted-foreground">
            No servers found.
          </p>
        ) : null}
      </div>
    </div>
  );
}
