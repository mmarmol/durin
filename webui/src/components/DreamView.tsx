import { useCallback, useEffect, useState } from "react";
import { Moon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DreamDrawer, type DrawerTarget } from "@/components/DreamDrawer";
import {
  fetchDreamDigest,
  fetchFlaggedPairs,
  listQuarantine,
  resolveFlaggedPair,
  runCronJob,
  type DreamDigest,
  type DreamEvent,
  type FlaggedPair,
  type QuarantineRow,
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
      ) : (
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
          onCountChange(p.length + q.length);
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
          onCountChange(next.length + quarantine.length);
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
    [token, quarantine.length, onCountChange, t],
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
    </div>
  );
}

interface DreamViewProps {
  onOpenSkills?: () => void;
}

export function DreamView({ onOpenSkills }: DreamViewProps) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [digest, setDigest] = useState<DreamDigest | null>(null);
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

  const handleClose = useCallback(() => setDrawerTarget(null), []);

  const handleRunNow = useCallback(async () => {
    setRunning(true);
    setRunError(null);
    try {
      await runCronJob(token, "memory_dream");
      const d = await fetchDreamDigest(token);
      setDigest(d);
    } catch {
      setRunError(t("dream.runError"));
    } finally {
      setRunning(false);
    }
  }, [token, t]);

  const handleBandejaCount = useCallback((count: number) => {
    setBandejaCount(count);
  }, []);

  const lastRun = digest?.last_run_at_ms
    ? relativeTime(digest.last_run_at_ms)
    : null;

  return (
    // position:relative so the drawer's absolute positioning is scoped here.
    <div className="relative flex h-full min-h-0 flex-col bg-background overflow-hidden">
      <header className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2">
        <Moon className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-sm font-semibold">{t("dream.title")}</h1>
        {lastRun ? (
          <span className="text-xs text-muted-foreground">
            {t("dream.lastRun", { time: lastRun })}
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
        ) : !digest || digest.events.length === 0 ? (
          <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
            {t("dream.empty")}
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto px-4 py-4">
            <div className="flex flex-col gap-2">
              {digest.events.map((ev, i) => (
                <EventCard
                  key={`${ev.at_ms}-${i}`}
                  event={ev}
                  onOpen={setDrawerTarget}
                />
              ))}
            </div>
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
