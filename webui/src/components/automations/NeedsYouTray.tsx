import { useTranslation } from "react-i18next";

import type { AutomationRun } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const ASK_CAP = 160;

function capText(text: string, max: number): string {
  const trimmed = text.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

/** The "needs you" inbox preview: every run parked on an approval or a
 *  question. `_park` (durin/automations/runtime.py) is the only place a run
 *  ever gets `status: "paused"`, and it always sets `ask_kind` to exactly
 *  "approval" or "question" — a stuck-goal escalation notice is a
 *  fire-and-forget message sent from `_on_stuck` on an already-finished
 *  run, never a pause, so it can never show up here as either kind.
 *
 *  Clicking Review/Answer only sets the selection — this component just
 *  tracks and highlights it (`data-selected`). The caller (AutomationsView)
 *  is what turns a selection into something actionable: it renders
 *  InboxView for the selected run right below this tray, the full
 *  approve/correct/reject or answer card. */
export function NeedsYouTray({
  runs,
  selectedRunId,
  onSelect,
}: {
  runs: AutomationRun[];
  selectedRunId: string | null;
  onSelect: (run: AutomationRun) => void;
}) {
  const { t } = useTranslation();
  const pending = runs.filter((r) => r.status === "paused");
  if (pending.length === 0) return null;

  return (
    <div className="rounded-lg border border-border">
      <div className="flex items-center gap-2 border-b border-border px-3.5 py-2.5 text-[13px] font-semibold">
        {t("automations.tray.title")}
        <span className="rounded-full bg-warn/15 px-2 py-0.5 text-[10.5px] font-medium text-warn">
          {t("automations.tray.pending", { count: pending.length })}
        </span>
      </div>
      <div className="flex flex-col">
        {pending.map((run) => {
          const isApproval = run.ask_kind === "approval";
          const text = capText(run.ask ?? run.proposal ?? "", ASK_CAP);
          const selected = selectedRunId === run.run_id;
          return (
            <div
              key={run.run_id}
              data-testid="tray-row"
              data-selected={selected ? "true" : undefined}
              className={cn(
                "flex items-center gap-3 border-t border-border px-3.5 py-2.5 first:border-t-0",
                selected && "bg-accent/40",
              )}
            >
              <span
                className={cn(
                  "shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-medium",
                  isApproval ? "bg-warn/15 text-warn" : "bg-accent/15 text-accent",
                )}
              >
                {t(isApproval ? "automations.tray.approval" : "automations.tray.question")}
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-medium">
                  {run.automation}
                  {text && <span className="font-normal text-muted-foreground"> — {text}</span>}
                </div>
                <div className="text-[11px] text-muted-foreground">{relativeTime(run.started_at * 1000)}</div>
              </div>
              <button
                type="button"
                onClick={() => onSelect(run)}
                className="shrink-0 rounded-full bg-primary px-3 py-1 text-[11.5px] font-semibold text-primary-foreground hover:bg-primary/90"
              >
                {t(isApproval ? "automations.tray.review" : "automations.tray.answer")}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
