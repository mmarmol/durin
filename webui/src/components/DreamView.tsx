import { useCallback, useEffect, useState } from "react";
import { Moon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DreamDrawer, type DrawerTarget } from "@/components/DreamDrawer";
import {
  fetchDreamDigest,
  fetchFlaggedPairs,
  fetchSkillSuggestions,
  runCronJob,
  type DreamDigest,
  type DreamEvent,
  type DreamLastRun,
} from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

function relativeTime(ms: number): string {
  const diff = Date.now() - ms;
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function kindDot(kind: string): string {
  if (kind === "run") return "#64748b"; // muted — a per-run summary, not a content change
  if (kind === "warning") return "#ef4444"; // degraded run / unparseable model output
  if (kind === "merged" || kind === "created" || kind === "flagged") return "#14b8a6";
  if (kind === "improved") return "#d97706";
  return "#14b8a6";
}

interface EventCardProps {
  event: DreamEvent;
  onOpen: (target: DrawerTarget) => void;
}

function EventCard({ event, onOpen }: EventCardProps) {
  const { t } = useTranslation();
  const color = kindDot(event.kind);
  const kindKey = `dream.kind.${event.kind}` as const;
  const kindLabel = t(kindKey, { defaultValue: event.kind });

  const hasRef =
    event.ref !== null &&
    (event.ref_kind === "entity" || event.ref_kind === "skill");

  function handleView() {
    if (hasRef) {
      onOpen({
        ref: event.ref as string,
        ref_kind: event.ref_kind as "entity" | "skill",
        summary: event.summary,
      });
    }
  }

  return (
    <div className="flex items-start gap-3 rounded-[8px] border border-border/40 bg-card px-4 py-3">
      <span
        className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
        style={{ backgroundColor: color }}
        aria-hidden
      />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            {kindLabel}
          </span>
          <span className="text-[11px] text-muted-foreground/60">
            {relativeTime(event.at_ms)}
          </span>
        </div>
        <p className="text-[13px] text-foreground">{event.summary}</p>
      </div>
      {hasRef ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="shrink-0 text-[12px]"
          onClick={handleView}
        >
          {t("dream.view")}
        </Button>
      ) : event.kind === "run" ? null : (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled
          className="shrink-0 text-[12px]"
        >
          {t("dream.view")}
        </Button>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[18px] font-semibold tabular-nums text-foreground">{value}</span>
      <span className="text-[11px] text-muted-foreground">{label}</span>
    </div>
  );
}

interface LastRunCardProps {
  lastRun: DreamLastRun | null;
  running: boolean;
}

/** The "última corrida" headline card — always shows what the most recent run
 * did (its counts), even when nothing changed (0/0/0/0), so the screen is never
 * blank after a run. While a run is in flight it shows a live "running" pulse. */
function LastRunCard({ lastRun, running }: LastRunCardProps) {
  const { t } = useTranslation();
  return (
    <div className="rounded-[10px] border border-border/50 bg-card px-4 py-3">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t("dream.lastRunTitle")}
        </span>
        {running ? (
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="h-2 w-2 animate-pulse rounded-full bg-[#14b8a6]" aria-hidden />
            {t("dream.running")}
          </span>
        ) : lastRun ? (
          <span className="text-[11px] text-muted-foreground/60">{relativeTime(lastRun.at_ms)}</span>
        ) : null}
      </div>
      {running && !lastRun ? (
        <p className="text-[13px] text-muted-foreground">{t("dream.runningEmpty")}</p>
      ) : lastRun ? (
        <div className="flex flex-wrap gap-x-5 gap-y-1.5">
          <Stat label={t("dream.stats.entities")} value={lastRun.entities} />
          <Stat label={t("dream.stats.merged")} value={lastRun.merged} />
          <Stat label={t("dream.stats.skillsCreated")} value={lastRun.skills_created} />
          <Stat label={t("dream.stats.skillsImproved")} value={lastRun.skills_improved} />
          <Stat label={t("dream.stats.sessions")} value={lastRun.sessions} />
        </div>
      ) : null}
    </div>
  );
}

/** The Pending sections that hold the dream's own decisions. */
export type DreamPendingSource = "flagged_pair" | "skill_suggestion";

interface DreamViewProps {
  /** Opens Pending at the section of the dream's decisions. */
  onOpenPending?: (source: DreamPendingSource) => void;
}

export function DreamView({ onOpenPending }: DreamViewProps) {
  const { token, client } = useClient();
  const { t } = useTranslation();
  const [digest, setDigest] = useState<DreamDigest | null>(null);
  const [liveEvents, setLiveEvents] = useState<DreamEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerTarget, setDrawerTarget] = useState<DrawerTarget | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  // The dream's decisions waiting in Pending: memory pairs it flagged and
  // skill changes it suggested. They are decided there, not here.
  const [decisions, setDecisions] = useState({ pairs: 0, suggestions: 0 });

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchDreamDigest(token)
      .then((d) => {
        if (!cancelled) setDigest(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const refreshDecisions = useCallback(() => {
    let cancelled = false;
    Promise.all([fetchFlaggedPairs(token), fetchSkillSuggestions(token)])
      .then(([pairs, suggestions]) => {
        if (!cancelled) setDecisions({ pairs: pairs.length, suggestions: suggestions.length });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => refreshDecisions(), [refreshDecisions]);

  // Live dream progress: drive the "running" indicator and prepend activity
  // items to the feed as the dream produces them. On run_finished, refetch the
  // (now-persisted) digest and drop the live items in the same swap so nothing
  // flickers or duplicates.
  useEffect(() => {
    const unsub = client.onDreamProgress((ev) => {
      if (ev.kind === "run_started") {
        setRunning(true);
        setRunError(null);
        setLiveEvents([]);
      } else if (ev.kind === "activity" && ev.item) {
        const item = ev.item as DreamEvent;
        setLiveEvents((prev) => [item, ...prev]);
      } else if (ev.kind === "run_finished") {
        if (ev.ok === false) setRunError(t("dream.runFailed"));
        // A run can flag new pairs and suggest new skill changes.
        refreshDecisions();
        fetchDreamDigest(token)
          .then((d) => {
            setDigest(d);
            setLiveEvents([]);
          })
          .catch(() => undefined)
          .finally(() => setRunning(false));
      }
    });
    return unsub;
  }, [client, token, t, refreshDecisions]);

  // Fallback: a run_finished frame can be missed if the socket drops mid-run.
  // Don't leave the indicator stuck — clear it after a generous ceiling.
  useEffect(() => {
    if (!running) return;
    const timer = setTimeout(() => setRunning(false), 20 * 60_000);
    return () => clearTimeout(timer);
  }, [running]);

  const handleClose = useCallback(() => setDrawerTarget(null), []);

  const handleRunNow = useCallback(async () => {
    setRunning(true);
    setRunError(null);
    setLiveEvents([]);
    try {
      // The cron run is async on the server (returns immediately). Live
      // dream_progress frames drive the feed and clear `running` on
      // run_finished — so we deliberately do NOT refetch the digest here.
      await runCronJob(token, "memory_dream");
    } catch {
      setRunError(t("dream.runError"));
      setRunning(false);
    }
  }, [token, t]);

  // Live items (this run) on top of the persisted digest, newest-first.
  const events = [...liveEvents, ...(digest?.events ?? [])];
  const waiting = decisions.pairs + decisions.suggestions;

  return (
    // position:relative so the drawer's absolute positioning is scoped here.
    <div className="relative flex h-full min-h-0 flex-col bg-background overflow-hidden">
      <header className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2">
        <Moon className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-sm font-semibold">{t("dream.title")}</h1>
        {running ? (
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="h-2 w-2 animate-pulse rounded-full bg-[#14b8a6]" aria-hidden />
            {t("dream.running")}
          </span>
        ) : null}
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={running}
          className="ml-auto"
          onClick={() => void handleRunNow()}
        >
          {running ? t("dream.running") : t("dream.runNow")}
        </Button>
        {runError ? (
          <span className="ml-2 text-xs text-destructive">{runError}</span>
        ) : null}
      </header>

      {waiting > 0 && onOpenPending ? (
        <button
          type="button"
          className="shrink-0 border-b border-border/40 px-4 py-2 text-left text-[13px] font-medium text-primary transition-colors hover:bg-accent/40"
          onClick={() => onOpenPending(decisions.pairs > 0 ? "flagged_pair" : "skill_suggestion")}
        >
          {t("dream.pendingDecisions", { count: waiting })}
        </button>
      ) : null}

      {loading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          {t("dream.loading")}
        </div>
      ) : error ? (
        <div className="flex flex-1 items-center justify-center text-sm text-destructive">
          {error}
        </div>
      ) : !digest?.last_run && events.length === 0 && !running ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          {t("dream.empty")}
        </div>
      ) : (
        <div className="flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-4">
          <LastRunCard lastRun={digest?.last_run ?? null} running={running} />
          {events.length > 0 ? (
            <div>
              <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                {t("dream.historyTitle")}
              </h2>
              <div className="flex flex-col gap-2">
                {events.map((ev, i) => (
                  <EventCard
                    key={`${ev.at_ms}-${i}`}
                    event={ev}
                    onOpen={setDrawerTarget}
                  />
                ))}
              </div>
            </div>
          ) : null}
        </div>
      )}

      <DreamDrawer target={drawerTarget} onClose={handleClose} />
    </div>
  );
}
