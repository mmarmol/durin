import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCw, ScrollText } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  ApiError,
  fetchLogs,
  setConfigValue,
  type LogLineRow,
  type LogFacets,
  type LogQueryParams,
} from "@/lib/api";
import { SettingsSectionTitle } from "./primitives";

type Tab = "gateway" | "telemetry";

/** Settings → Logs section. Read-only viewer over the gateway JSONL log
 *  and the existing telemetry JSONL files. Two tabs share one server read
 *  primitive; they differ only in filters and columns. The telemetry
 *  backend is never mutated here. */
export function LogsSettings({ token }: { token: string }) {
  const [tab, setTab] = useState<Tab>("gateway");
  return (
    <div className="space-y-6">
      <SettingsSectionTitle>
        <span className="flex items-center gap-2">
          <ScrollText className="h-4 w-4" aria-hidden /> Logs
        </span>
      </SettingsSectionTitle>
      <div className="flex gap-2">
        {(["gateway", "telemetry"] as Tab[]).map((k) => (
          <Button
            key={k}
            size="sm"
            variant={tab === k ? "default" : "ghost"}
            className="rounded-full capitalize"
            onClick={() => setTab(k)}
          >
            {k}
          </Button>
        ))}
      </div>
      <LogTable key={tab} token={token} source={tab} />
    </div>
  );
}

function LogTable({ token, source }: { token: string; source: Tab }) {
  const [rows, setRows] = useState<LogLineRow[]>([]);
  const [facets, setFacets] = useState<LogFacets>({});
  const [cursor, setCursor] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [windowHours, setWindowHours] = useState<number | "all">(24);
  const [q, setQ] = useState("");
  const [level, setLevel] = useState<string[]>([]);
  const [channel, setChannel] = useState<string[]>([]);
  const [session, setSession] = useState<string[]>([]);
  const [type, setType] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);

  const baseParams = useMemo<LogQueryParams>(() => ({
    source,
    q: q.trim() || undefined,
    level: source === "gateway" ? level : undefined,
    channel: source === "gateway" ? channel : undefined,
    session: source === "telemetry" ? session : undefined,
    type: source === "telemetry" ? type : undefined,
    windowHours,
    limit: 200,
  }), [source, q, level, channel, session, type, windowHours]);

  const load = useCallback(async (append: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const page = await fetchLogs(token, {
        ...baseParams,
        beforeTs: append ? cursor : null,
      });
      setRows((prev) => (append ? [...prev, ...page.lines] : page.lines));
      setFacets(page.facets);
      setCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token, baseParams, cursor]);

  // Reload from scratch whenever filters change.
  useEffect(() => {
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseParams]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search…"
          className="h-8 flex-1 min-w-[180px] rounded-md border bg-background px-2 text-[13px]"
        />
        {source === "gateway" ? (
          <MultiSelect label="level" options={facets.levels ?? []} value={level} onChange={setLevel} />
        ) : (
          <MultiSelect label="type" options={facets.types ?? []} value={type} onChange={setType} />
        )}
        {source === "gateway" ? (
          <MultiSelect label="channel" options={facets.channels ?? []} value={channel} onChange={setChannel} />
        ) : (
          <MultiSelect label="session" options={facets.sessions ?? []} value={session} onChange={setSession} />
        )}
        <Button size="sm" variant="ghost" onClick={() => void load(false)} disabled={loading} className="rounded-full">
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>

      {source === "gateway" ? <GatewayConfigRow token={token} /> : null}

      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

      <div className="max-h-[60vh] overflow-auto rounded-md border font-mono text-[12px]">
        {rows.map((r, i) => (
          <div key={i} className="border-b last:border-0">
            <button
              className="flex w-full gap-3 px-2 py-1 text-left hover:bg-muted/50"
              onClick={() => setExpanded(expanded === i ? null : i)}
            >
              <span className="tabular-nums text-muted-foreground">
                {new Date(r.ts * 1000).toLocaleString()}
              </span>
              {source === "gateway" ? (
                <>
                  <span className="w-14 shrink-0">{String(r.fields.level ?? "")}</span>
                  <span className="w-24 shrink-0 text-muted-foreground">{String(r.fields.channel ?? "")}</span>
                  <span className="truncate">{String(r.fields.message ?? "")}</span>
                </>
              ) : (
                <>
                  <span className="w-32 shrink-0 text-muted-foreground">{String(r.fields.session ?? "")}</span>
                  <span className="w-48 shrink-0">{String(r.fields.type ?? "")}</span>
                  <span className="truncate">{JSON.stringify(r.fields.data ?? {})}</span>
                </>
              )}
            </button>
            {expanded === i ? (
              <pre className="overflow-auto bg-muted/40 px-3 py-2 text-[11px]">
                {JSON.stringify(r.raw, null, 2)}
              </pre>
            ) : null}
          </div>
        ))}
        {rows.length === 0 && !loading ? (
          <p className="px-2 py-4 text-muted-foreground">No log lines in this window.</p>
        ) : null}
      </div>

      {hasMore ? (
        <div className="flex items-center gap-3">
          <Button size="sm" variant="ghost" className="rounded-full" disabled={loading}
            onClick={() => void load(true)}>
            Load older
          </Button>
          {windowHours !== "all" ? (
            <button className="text-[12px] text-muted-foreground underline"
              onClick={() => setWindowHours((w) => (w === "all" ? "all" : w === 24 ? 168 : "all"))}>
              Searched last {windowHours}h — widen
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function GatewayConfigRow({ token }: { token: string }) {
  const [mb, setMb] = useState("");
  const [days, setDays] = useState("");
  const [saved, setSaved] = useState(false);
  const save = async (key: string, value: number) => {
    await setConfigValue(token, key, value);
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border bg-muted/30 px-2 py-1 text-[12px]">
      <span className="text-muted-foreground">Rotation</span>
      <input value={mb} onChange={(e) => setMb(e.target.value)} placeholder="max MB"
        className="h-7 w-20 rounded border bg-background px-1" />
      <Button size="sm" variant="ghost" className="rounded-full"
        disabled={!mb} onClick={() => void save("logging.max_file_mb", Number(mb))}>set</Button>
      <span className="text-muted-foreground">Retention</span>
      <input value={days} onChange={(e) => setDays(e.target.value)} placeholder="days"
        className="h-7 w-20 rounded border bg-background px-1" />
      <Button size="sm" variant="ghost" className="rounded-full"
        disabled={!days} onClick={() => void save("logging.retention_days", Number(days))}>set</Button>
      {saved ? <span className="text-emerald-600">saved</span> : null}
    </div>
  );
}

function MultiSelect({
  label, options, value, onChange,
}: { label: string; options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  const toggle = (opt: string) =>
    onChange(value.includes(opt) ? value.filter((v) => v !== opt) : [...value, opt]);
  return (
    <details className="relative">
      <summary className="h-8 cursor-pointer list-none rounded-md border bg-background px-2 text-[12px] leading-8">
        {label}{value.length ? ` (${value.length})` : ""}
      </summary>
      <div className="absolute z-10 mt-1 max-h-60 w-48 overflow-auto rounded-md border bg-popover p-1 shadow">
        {options.map((opt) => (
          <label key={opt} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-[12px] hover:bg-muted">
            <input type="checkbox" checked={value.includes(opt)} onChange={() => toggle(opt)} />
            <span className="truncate">{opt}</span>
          </label>
        ))}
        {options.length === 0 ? <span className="px-1 text-[11px] text-muted-foreground">—</span> : null}
      </div>
    </details>
  );
}
