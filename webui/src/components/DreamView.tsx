import { useEffect, useState } from "react";
import { Moon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { fetchDreamDigest, type DreamDigest, type DreamEvent } from "@/lib/api";
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
  if (kind === "quarantined") return "#dc2626";
  return "#14b8a6";
}

function EventCard({ event }: { event: DreamEvent }) {
  const { t } = useTranslation();
  const color = kindDot(event.kind);
  const kindKey = `dream.kind.${event.kind}` as const;
  const kindLabel = t(kindKey, { defaultValue: event.kind });
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
      <Button
        type="button"
        variant="ghost"
        size="sm"
        disabled
        className="shrink-0 text-[12px]"
      >
        {t("dream.view")}
      </Button>
    </div>
  );
}

export function DreamView() {
  const { token } = useClient();
  const { t } = useTranslation();
  const [digest, setDigest] = useState<DreamDigest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  const lastRun = digest?.last_run_at_ms
    ? relativeTime(digest.last_run_at_ms)
    : null;

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
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
          disabled
          className="ml-auto"
        >
          {t("dream.runNow")}
        </Button>
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
              <EventCard key={`${ev.at_ms}-${i}`} event={ev} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
