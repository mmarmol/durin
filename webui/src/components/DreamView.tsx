import { useCallback, useEffect, useState } from "react";
import { Moon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DreamDrawer, type DrawerTarget } from "@/components/DreamDrawer";
import {
  fetchDreamDigest,
  fetchFlaggedPairs,
  fetchSkillSuggestions,
  acceptSkillSuggestion,
  rejectSkillSuggestion,
  listQuarantine,
  resolveFlaggedPair,
  runCronJob,
  type DreamDigest,
  type DreamEvent,
  type DreamLastRun,
  type FlaggedPair,
  type QuarantineRow,
  type SkillSuggestion,
} from "@/lib/api";
import { DiffViewer } from "./DiffViewer";
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

interface FlaggedPairCardProps {
  pair: FlaggedPair;
  onOpen: (target: DrawerTarget) => void;
  onResolve: (pair: FlaggedPair, action: "merge" | "separate") => void;
  resolving: boolean;
}

function FlaggedPairCard({ pair, onOpen, onResolve, resolving }: FlaggedPairCardProps) {
  const { t } = useTranslation();

  function handleView() {
    onOpen({
      ref: pair.ref_a,
      ref_kind: "entity",
      summary: pair.reasoning,
    });
  }

  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-border/40 bg-card px-4 py-3">
      <div className="flex items-start gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[12px] font-medium text-foreground">{pair.ref_a}</span>
            <span className="text-[11px] text-muted-foreground">↔</span>
            <span className="text-[12px] font-medium text-foreground">{pair.ref_b}</span>
            <span className="text-[11px] text-muted-foreground/60">
              {pair.verdict} · {pair.confidence}%
            </span>
          </div>
          <p className="text-[13px] text-muted-foreground mt-1">{pair.reasoning}</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="shrink-0 text-[12px]"
          onClick={handleView}
        >
          {t("dream.view")}
        </Button>
      </div>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="default"
          size="sm"
          className="text-[12px]"
          disabled={resolving}
          onClick={() => onResolve(pair, "merge")}
        >
          {t("dream.bandeja.merge")}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="text-[12px]"
          disabled={resolving}
          onClick={() => onResolve(pair, "separate")}
        >
          {t("dream.bandeja.keepSeparate")}
        </Button>
      </div>
    </div>
  );
}

interface QuarantineCardProps {
  skill: QuarantineRow;
  onOpen: (target: DrawerTarget) => void;
  onOpenSkills?: () => void;
}

function QuarantineCard({ skill, onOpen, onOpenSkills }: QuarantineCardProps) {
  const { t } = useTranslation();

  const verdictSummary = skill.findings.length > 0
    ? skill.findings.map((f) => f.detail).join("; ")
    : skill.verdict;

  function handleView() {
    onOpen({
      ref: skill.name,
      ref_kind: "skill",
      summary: verdictSummary,
    });
  }

  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-border/40 bg-card px-4 py-3">
      <div className="flex items-start gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <span className="text-[12px] font-medium text-foreground">{skill.name}</span>
            <span className="text-[11px] text-muted-foreground/70">{skill.verdict}</span>
          </div>
          <p className="text-[13px] text-muted-foreground mt-0.5 line-clamp-2">{verdictSummary}</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="shrink-0 text-[12px]"
          onClick={handleView}
        >
          {t("dream.view")}
        </Button>
      </div>
      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="text-[12px]"
          onClick={() => onOpenSkills?.()}
        >
          {t("dream.bandeja.reviewInSkills")}
        </Button>
      </div>
    </div>
  );
}

export function SkillSuggestionsSection({
  token,
  onCountChange,
}: {
  token: string;
  onCountChange: (n: number) => void;
}) {
  const { t } = useTranslation();
  const [items, setItems] = useState<SkillSuggestion[]>([]);
  const [busy, setBusy] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    fetchSkillSuggestions(token).then((s) => {
      if (!cancelled) {
        setItems(s);
        onCountChange(s.length);
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const resolve = useCallback(
    async (id: string, action: "accept" | "reject") => {
      setBusy((p) => new Set(p).add(id));
      try {
        if (action === "accept") await acceptSkillSuggestion(token, id);
        else await rejectSkillSuggestion(token, id);
        setItems((prev) => {
          const next = prev.filter((s) => s.id !== id);
          onCountChange(next.length);
          return next;
        });
      } finally {
        setBusy((p) => {
          const n = new Set(p);
          n.delete(id);
          return n;
        });
      }
    },
    [token, onCountChange],
  );

  return (
    <section>
      <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t("dream.bandeja.suggestionsTitle")}
      </h2>
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {t("dream.bandeja.emptySuggestions")}
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((s) => (
            <div
              key={s.id}
              className="flex flex-col gap-2 rounded-[8px] border border-border/40 bg-card px-4 py-3"
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-[12px] font-medium text-foreground">{s.skill}</span>
                <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold text-amber-600 dark:text-amber-400">
                  {t(`dream.bandeja.action.${s.type}`)}
                </span>
              </div>
              <p className="text-[13px] text-muted-foreground">{s.reason}</p>
              {s.patch ? <DiffViewer patch={s.patch} /> : null}
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="default"
                  size="sm"
                  className="text-[12px]"
                  disabled={busy.has(s.id)}
                  onClick={() => resolve(s.id, "accept")}
                >
                  {t("dream.bandeja.accept")}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="text-[12px]"
                  disabled={busy.has(s.id)}
                  onClick={() => resolve(s.id, "reject")}
                >
                  {t("dream.bandeja.reject")}
                </Button>
                <span className="ml-auto text-[11px] text-muted-foreground">
                  {t("dream.bandeja.rejectHint")}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

interface BandejaTabProps {
  onOpen: (target: DrawerTarget) => void;
  onOpenSkills?: () => void;
  onCountChange: (count: number) => void;
}

function BandejaTab({ onOpen, onOpenSkills, onCountChange }: BandejaTabProps) {
  const { token } = useClient();
  const { t } = useTranslation();

  const [pairs, setPairs] = useState<FlaggedPair[]>([]);
  const [quarantine, setQuarantine] = useState<QuarantineRow[]>([]);
  const [suggestionsCount, setSuggestionsCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [resolvingKeys, setResolvingKeys] = useState<Set<string>>(new Set());
  const [resolveError, setResolveError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([fetchFlaggedPairs(token), listQuarantine(token)])
      .then(([p, q]) => {
        if (!cancelled) {
          setPairs(p);
          setQuarantine(q);
          onCountChange(p.length + q.length + suggestionsCount);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  // onCountChange is stable (useCallback in parent) so it's safe to include
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const handleResolve = useCallback(
    async (pair: FlaggedPair, action: "merge" | "separate") => {
      const key = `${pair.ref_a}:${pair.ref_b}`;
      setResolvingKeys((prev) => new Set(prev).add(key));
      setResolveError(null);
      try {
        await resolveFlaggedPair(token, { ref_a: pair.ref_a, ref_b: pair.ref_b, action });
        setPairs((prev) => {
          const next = prev.filter((p) => !(p.ref_a === pair.ref_a && p.ref_b === pair.ref_b));
          onCountChange(next.length + quarantine.length + suggestionsCount);
          return next;
        });
      } catch {
        setResolveError(t("dream.bandeja.resolveError"));
      } finally {
        setResolvingKeys((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
      }
    },
    [token, quarantine.length, suggestionsCount, onCountChange, t],
  );

  const handleSuggestionsCount = useCallback(
    (n: number) => {
      setSuggestionsCount(n);
      onCountChange(pairs.length + quarantine.length + n);
    },
    [pairs.length, quarantine.length, onCountChange],
  );

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
        {t("dream.loading")}
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-6">
      {resolveError && (
        <p className="text-sm text-destructive">{resolveError}</p>
      )}
      <section>
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t("dream.bandeja.flaggedTitle")}
        </h2>
        {pairs.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("dream.bandeja.emptyFlagged")}</p>
        ) : (
          <div className="flex flex-col gap-2">
            {pairs.map((pair) => {
              const key = `${pair.ref_a}:${pair.ref_b}`;
              return (
                <FlaggedPairCard
                  key={key}
                  pair={pair}
                  onOpen={onOpen}
                  onResolve={handleResolve}
                  resolving={resolvingKeys.has(key)}
                />
              );
            })}
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t("dream.bandeja.quarantineTitle")}
        </h2>
        {quarantine.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("dream.bandeja.emptyQuarantine")}</p>
        ) : (
          <div className="flex flex-col gap-2">
            {quarantine.map((skill) => (
              <QuarantineCard
                key={skill.name}
                skill={skill}
                onOpen={onOpen}
                onOpenSkills={onOpenSkills}
              />
            ))}
          </div>
        )}
      </section>

      <SkillSuggestionsSection token={token} onCountChange={handleSuggestionsCount} />
    </div>
  );
}

interface DreamViewProps {
  onOpenSkills?: () => void;
}

export function DreamView({ onOpenSkills }: DreamViewProps) {
  const { token, client } = useClient();
  const { t } = useTranslation();
  const [digest, setDigest] = useState<DreamDigest | null>(null);
  const [liveEvents, setLiveEvents] = useState<DreamEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerTarget, setDrawerTarget] = useState<DrawerTarget | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"resumen" | "bandeja">("resumen");
  const [bandejaCount, setBandejaCount] = useState(0);

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

  // Fetch the Bandeja count on load so the tab badge surfaces pending items
  // without the user having to open the tab first (the count lived inside
  // BandejaTab, which only mounts when that tab is active). BandejaTab still
  // refetches and keeps the count live while it is open.
  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchFlaggedPairs(token), listQuarantine(token), fetchSkillSuggestions(token)])
      .then(([p, q, s]) => {
        if (!cancelled) setBandejaCount(p.length + q.length + s.length);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [token]);

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
  }, [client, token, t]);

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

  const handleBandejaCount = useCallback((count: number) => {
    setBandejaCount(count);
  }, []);

  // Live items (this run) on top of the persisted digest, newest-first.
  const events = [...liveEvents, ...(digest?.events ?? [])];

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

      <div className="flex shrink-0 border-b border-border/40 px-3">
        <button
          type="button"
          className={`px-3 py-2 text-sm font-medium transition-colors ${
            activeTab === "resumen"
              ? "border-b-2 border-primary text-foreground"
              : "text-muted-foreground hover:text-foreground"
          }`}
          onClick={() => setActiveTab("resumen")}
        >
          {t("dream.tabs.resumen")}
        </button>
        <button
          type="button"
          className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium transition-colors ${
            activeTab === "bandeja"
              ? "border-b-2 border-primary text-foreground"
              : "text-muted-foreground hover:text-foreground"
          }`}
          onClick={() => setActiveTab("bandeja")}
        >
          {t("dream.tabs.bandeja")}
          {bandejaCount > 0 && (
            <span className="rounded-full bg-primary/20 px-1.5 py-0.5 text-[10px] font-semibold text-primary leading-none">
              {bandejaCount}
            </span>
          )}
        </button>
      </div>

      {activeTab === "resumen" ? (
        loading ? (
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
        )
      ) : (
        <BandejaTab
          onOpen={setDrawerTarget}
          onOpenSkills={onOpenSkills}
          onCountChange={handleBandejaCount}
        />
      )}

      <DreamDrawer target={drawerTarget} onClose={handleClose} />
    </div>
  );
}
