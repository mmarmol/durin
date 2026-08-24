import { useTranslation } from "react-i18next";

import type { AutomationSummary } from "@/lib/api";
import { cn } from "@/lib/utils";

const base = "inline-flex shrink-0 items-center whitespace-nowrap rounded-full px-2 py-0.5 text-[10.5px] font-medium";

/** The automation's life state, in priority order: an achieved goal wins
 *  even over a life config that's technically still "set" (the spec
 *  disables itself on achieving); a stuck automation reads as an attention
 *  chip regardless of its `on_stuck` mode (notify/keep also set `stuck`,
 *  per AutomationsService._life_state — only the pause behavior differs,
 *  not the honesty of the chip); a configured-but-not-yet-stuck goal shows
 *  the running attempt count; with no life config at all, the chip just
 *  reflects enabled/disabled. */
export function LifeChip({ automation }: { automation: AutomationSummary }) {
  const { t } = useTranslation();

  if (automation.achieved) {
    return <span className={cn(base, "bg-primary/15 text-primary")}>{t("automations.list.life.achieved")}</span>;
  }
  if (automation.stuck) {
    return <span className={cn(base, "bg-destructive/15 text-destructive")}>{t("automations.list.life.stuck")}</span>;
  }
  if (automation.life) {
    const label =
      automation.attempts <= 0
        ? t("automations.list.life.goal")
        : automation.life.max_attempts != null
          ? t("automations.list.life.goalAttempt", { attempt: automation.attempts, max: automation.life.max_attempts })
          : t("automations.list.life.goalAttemptOpen", { attempt: automation.attempts });
    return <span className={cn(base, "bg-warn/15 text-warn")}>{label}</span>;
  }
  return (
    <span className={cn(base, automation.enabled ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground")}>
      {t(automation.enabled ? "automations.list.life.active" : "automations.list.life.paused")}
    </span>
  );
}
