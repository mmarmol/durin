import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { AnswerInput, expandableRowProps, RetryButton, WaitingAnswerToggle } from "@/components/loops/RunControls";
import { RunDetail } from "@/components/loops/RunDetail";
import type { LoopRun } from "@/lib/api";
import { relativeTime } from "@/lib/format";

const CASE_SNIPPET_CAP = 60;

// States are system-owned, so the board is read-only placement — no
// drag-and-drop. A run's status maps to exactly one of these five columns;
// no_goal/escalated/error all land in "attention" since each needs a look
// from the operator, just for different reasons.
type ColumnId = "needs_operator" | "waiting_info" | "running" | "done" | "attention";

const COLUMNS: ColumnId[] = ["needs_operator", "waiting_info", "running", "done", "attention"];

const COLUMN_LABEL_KEY: Record<ColumnId, string> = {
  needs_operator: "loops.activity.status.needs_operator",
  waiting_info: "loops.activity.status.waiting_info",
  running: "loops.activity.status.running",
  done: "loops.activity.status.done",
  attention: "loops.activity.status.attention",
};

function columnFor(status: LoopRun["status"]): ColumnId {
  switch (status) {
    case "needs_operator":
    case "waiting_info":
    case "running":
    case "done":
      return status;
    default:
      return "attention";
  }
}

// Case identity: what this run is about, since the column already carries
// the status. Prefer the channel origin's subject (the human-facing
// reference for a channel-sourced run); otherwise a capped task snippet.
function caseIdentity(run: LoopRun): string {
  if (run.origin?.subject) return run.origin.subject;
  const task = run.task || "";
  return task.length > CASE_SNIPPET_CAP ? task.slice(0, CASE_SNIPPET_CAP) + "…" : task;
}

function BoardCard({
  run,
  expanded,
  onToggle,
  children,
}: {
  run: LoopRun;
  expanded: boolean;
  onToggle: () => void;
  children?: ReactNode;
}) {
  const identity = caseIdentity(run);
  return (
    <div
      className="flex flex-col gap-1.5 rounded-md border border-border bg-background px-3 py-2 text-xs"
      {...expandableRowProps(onToggle)}
    >
      <div className="flex flex-col gap-0.5">
        <span className="font-mono font-medium">{run.loop}</span>
        {!!identity && <span className="truncate text-muted-foreground">{identity}</span>}
        {!!run.started_at && (
          <span className="text-[10px] text-muted-foreground">{relativeTime(run.started_at * 1000)}</span>
        )}
      </div>
      {children}
      {expanded && (
        // Stop propagation: clicks inside the expanded detail (e.g. the
        // copyable run-id button, "show more") must not re-toggle the card.
        <div className="rounded-md border border-border/60 bg-muted/20 px-2 py-1.5" onClick={(e) => e.stopPropagation()}>
          <RunDetail run={run} />
        </div>
      )}
    </div>
  );
}

export function BoardView({
  runs,
  onAnswer,
  answeringId,
  onRetry,
  retryingId,
  expandedId,
  onToggle,
}: {
  runs: LoopRun[];
  onAnswer: (run: LoopRun, answer: string) => Promise<boolean>;
  answeringId: string | null;
  onRetry: (run: LoopRun) => void;
  retryingId: string | null;
  expandedId: string | null;
  onToggle: (runId: string) => void;
}) {
  const { t } = useTranslation();

  const grouped: Record<ColumnId, LoopRun[]> = {
    needs_operator: [],
    waiting_info: [],
    running: [],
    done: [],
    attention: [],
  };
  for (const run of runs) {
    grouped[columnFor(run.status)].push(run);
  }
  for (const col of COLUMNS) {
    grouped[col].sort((a, b) => b.started_at - a.started_at);
  }

  return (
    <div className="flex w-full gap-3 overflow-x-auto">
      {COLUMNS.map((col) => (
        <div
          key={col}
          className="flex w-64 shrink-0 flex-col gap-2"
          role="group"
          aria-label={t(COLUMN_LABEL_KEY[col])}
        >
          <div className="flex items-center gap-1.5 px-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            <span>{t(COLUMN_LABEL_KEY[col])}</span>
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] normal-case tracking-normal">
              {grouped[col].length}
            </span>
          </div>
          <div className="flex flex-col gap-2">
            {grouped[col].map((run) => (
              <BoardCard key={run.run_id} run={run} expanded={expandedId === run.run_id} onToggle={() => onToggle(run.run_id)}>
                {run.status === "needs_operator" && (
                  <>
                    {!!run.ask && <div className="whitespace-pre-wrap break-words">{run.ask}</div>}
                    <AnswerInput run={run} onAnswer={onAnswer} answering={answeringId === run.run_id} />
                  </>
                )}
                {run.status === "waiting_info" && (
                  <>
                    {!!run.ask && <div className="whitespace-pre-wrap break-words text-foreground/80">{run.ask}</div>}
                    <WaitingAnswerToggle run={run} onAnswer={onAnswer} answering={answeringId === run.run_id} />
                  </>
                )}
                {run.status === "escalated" && (
                  <RetryButton run={run} onRetry={onRetry} retrying={retryingId === run.run_id} />
                )}
              </BoardCard>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
