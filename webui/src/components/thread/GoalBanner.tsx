import { Target } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { GoalStateWsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

export function GoalBanner({ goal }: { goal: GoalStateWsPayload | undefined }) {
  const { t } = useTranslation();
  if (!goal || !goal.active) return null;
  return (
    <div
      className={cn(
        "flex items-center gap-2 border-b border-border/60 bg-muted/40 px-4 py-1.5 text-[12.5px]",
      )}
      role="status"
      aria-label={t("goal.banner.label")}
    >
      <Target className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
      {goal.objective ? <span className="font-medium">{goal.objective}</span> : null}
      {goal.ui_summary ? (
        <span className="ml-auto shrink-0 text-muted-foreground">{goal.ui_summary}</span>
      ) : null}
    </div>
  );
}
