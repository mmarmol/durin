import { useCallback, useEffect, useState } from "react";
import { Moon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DreamDrawer, type DrawerTarget } from "@/components/DreamDrawer";
import { fetchDreamDigest, runCronJob, type DreamDigest, type DreamEvent } from "@/lib/api";
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

export function DreamView() {
  const { token } = useClient();
  const { t } = useTranslation();
  const [digest, setDigest] = useState<DreamDigest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerTarget, setDrawerTarget] = useState<DrawerTarget | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

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

      {loading ? (
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
      )}

      <DreamDrawer target={drawerTarget} onClose={handleClose} />
    </div>
  );
}
