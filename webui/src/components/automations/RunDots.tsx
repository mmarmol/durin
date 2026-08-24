import { useTranslation } from "react-i18next";

import type { AutomationRun } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const MAX_DOTS = 5;

// achieved/completed read as the same "it worked" tone as loops' own
// OutcomeStrip; rejected/interrupted are not failures (a human or an
// operator ended them on purpose) so they get the warn tone rather than
// destructive; paused shares running's accent (something is actively
// holding a slot) but without the pulse, which is reserved for "ticking
// right now".
function toneFor(status: AutomationRun["status"]): string {
  if (status === "achieved" || status === "completed") return "bg-primary";
  if (status === "failed") return "bg-destructive";
  if (status === "rejected" || status === "interrupted") return "bg-warn";
  if (status === "running") return "bg-accent animate-pulse";
  return "bg-accent";
}

/** The last few runs for one automation as a compact dot strip. Renders
 *  nothing when there are no runs to show — a decorative, best-effort
 *  widget, not a load-bearing status. The caller controls both filtering
 *  (which automation) and order (this only ever takes the first 5 it's
 *  given). */
export function RunDots({ runs }: { runs: AutomationRun[] }) {
  const { t } = useTranslation();
  const dots = runs.slice(0, MAX_DOTS);
  if (dots.length === 0) return null;
  return (
    <div className="flex items-center gap-1" data-testid="run-dots">
      {dots.map((run) => (
        <span
          key={run.run_id}
          data-testid="run-dot"
          title={`${t(`automations.list.runStatus.${run.status}`, run.status)} · ${relativeTime(run.started_at * 1000)}`}
          className={cn("h-1.5 w-1.5 rounded-full", toneFor(run.status))}
        />
      ))}
    </div>
  );
}
