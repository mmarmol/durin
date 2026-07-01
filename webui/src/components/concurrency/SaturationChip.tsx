import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { useConcurrencySnapshot } from "@/hooks/useConcurrencySnapshot";

/** Always-visible aggregate: "5 / 12" ceiling occupancy, amber when work is
 * queued. Click behavior is caller-defined via onOpen. Hidden until the
 * first snapshot arrives so it never shows a misleading 0/0. */
export function SaturationChip({ onOpen }: { onOpen: () => void }) {
  const { t } = useTranslation();
  const snap = useConcurrencySnapshot();
  if (!snap) return null;

  const { active, limit } = snap.lanes.ceiling;
  const queued = snap.queued;
  const unlimited = limit === 0;
  const label = unlimited
    ? t("concurrency.chip.unlimited", { active })
    : t("concurrency.chip.label", { active, limit });

  return (
    <button
      type="button"
      onClick={onOpen}
      title={queued > 0 ? `${label} · ${t("concurrency.chip.queued", { count: queued })}` : label}
      aria-label={label}
      className={cn(
        "flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[12px] tabular-nums transition-colors",
        queued > 0
          ? "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400"
          : "border-border/60 bg-muted/30 text-muted-foreground hover:text-foreground",
      )}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          queued > 0 ? "bg-amber-500" : active > 0 ? "bg-emerald-500" : "bg-muted-foreground/40",
        )}
        aria-hidden
      />
      <span>{unlimited ? `${active}` : `${active} / ${limit}`}</span>
    </button>
  );
}
