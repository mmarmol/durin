import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import type { LoopRun, LoopRunCheck } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const TASK_CAP = 240;
const REF_CAP = 40;

// Mirrors ActivityView's statusTone (kept local rather than imported, since
// ActivityView renders this component and importing back would be circular):
// needs_operator/waiting_info read as actionable, escalated/error as
// destructive, everything else as quiet muted text.
function statusTone(status: LoopRun["status"]): string {
  if (status === "needs_operator") return "bg-accent text-accent-foreground";
  if (status === "waiting_info") return "bg-primary/15 text-primary";
  if (status === "escalated" || status === "error") return "text-destructive";
  return "text-muted-foreground";
}

// A loop run's workflow_run_id has no in-app viewer to open it by key — the
// Workflows -> Runs view only tracks which run is open via that component's
// own local state (no route or prop lets another view deep-link into a
// specific run_id). Surface it as a labelled, copyable reference instead so
// an auditor can look it up by hand in the Workflows tab.
function CopyableRunId({ value }: { value: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const onCopy = () => {
    if (!navigator.clipboard) return;
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1_500);
    });
  };
  return (
    <button
      type="button"
      onClick={onCopy}
      title={t("loops.runDetail.copyWorkflowRun")}
      aria-label={copied ? t("loops.runDetail.workflowRunCopied") : t("loops.runDetail.copyWorkflowRun")}
      className="inline-flex max-w-full items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
    >
      {copied ? <Check className="h-3 w-3 shrink-0" /> : <Copy className="h-3 w-3 shrink-0" />}
      <span className="truncate">{value}</span>
    </button>
  );
}

function CheckRow({ check }: { check: LoopRunCheck }) {
  const ref = check.ref.length > REF_CAP ? check.ref.slice(0, REF_CAP) + "…" : check.ref;
  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded border border-border px-2 py-1">
      <span className="rounded bg-muted px-1 py-0.5 text-[10px]">{check.kind}</span>
      <span className="truncate font-mono text-[11px] text-muted-foreground" title={check.ref}>
        {ref}
      </span>
      <span className={cn("rounded px-1 py-0.5 text-[10px]", check.passed ? "bg-muted" : "bg-destructive/10 text-destructive")}>
        {check.passed ? "✓" : "✗"}
      </span>
      {check.detail && (
        <span className="whitespace-pre-wrap break-words text-[11px] text-muted-foreground">{check.detail}</span>
      )}
    </div>
  );
}

// The detail view of a single loop run, shown expanded under its ActivityView
// row: status + timestamps, origin (who/what triggered it), the task (capped,
// expandable), the ask, an error detail (destructive tone), the goal checks
// table, and a copyable reference to the underlying workflow run.
export function RunDetail({ run }: { run: LoopRun }) {
  const { t } = useTranslation();
  const [taskExpanded, setTaskExpanded] = useState(false);

  const task = run.task || "";
  const taskOverCap = task.length > TASK_CAP;
  const taskText = taskOverCap && !taskExpanded ? task.slice(0, TASK_CAP) + "…" : task;

  return (
    <div className="flex flex-col gap-2 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={cn("rounded-full px-1.5 py-0.5 text-[10px]", statusTone(run.status))}>
          {t("loops.activity.status." + run.status, run.status)}
        </span>
        {!!run.started_at && (
          <span className="text-[10px] text-muted-foreground">
            {t("loops.runDetail.started", { when: relativeTime(run.started_at * 1000) })}
          </span>
        )}
        {!!run.finished_at && (
          <span className="text-[10px] text-muted-foreground">
            {t("loops.runDetail.finished", { when: relativeTime(run.finished_at * 1000) })}
          </span>
        )}
      </div>

      {run.origin && (
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="rounded bg-muted px-1.5 py-0.5">{run.origin.channel}</span>
          <span>{run.origin.sender}</span>
          {!!run.origin.subject && (
            <>
              <span>·</span>
              <span className="truncate">{run.origin.subject}</span>
            </>
          )}
          {!!run.origin.thread && (
            <span className="font-mono text-[10px]" title={run.origin.thread}>
              {run.origin.thread.slice(0, 8)}
            </span>
          )}
        </div>
      )}

      {!!task && (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("loops.runDetail.taskLabel")}
          </span>
          <div className="whitespace-pre-wrap break-words">{taskText}</div>
          {taskOverCap && (
            <Button
              size="sm"
              variant="ghost"
              className="h-6 w-fit gap-1 px-2 text-[11px] text-muted-foreground"
              onClick={() => setTaskExpanded((v) => !v)}
            >
              {taskExpanded ? t("loops.runDetail.showLess") : t("loops.runDetail.showMore")}
            </Button>
          )}
        </div>
      )}

      {!!run.ask && (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("loops.runDetail.askLabel")}
          </span>
          <div className="whitespace-pre-wrap break-words">{run.ask}</div>
        </div>
      )}

      {!!run.detail && (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("loops.runDetail.detailLabel")}
          </span>
          <div className="whitespace-pre-wrap break-words text-destructive">{run.detail}</div>
        </div>
      )}

      {run.checks != null && (
        <div className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("loops.runDetail.checksTitle")}
          </span>
          {run.checks.length === 0 ? (
            <span className="text-muted-foreground">{t("loops.runDetail.noChecks")}</span>
          ) : (
            run.checks.map((c, i) => <CheckRow key={i} check={c} />)
          )}
        </div>
      )}

      {!!run.workflow_run_id && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("loops.runDetail.workflowRunLabel")}
          </span>
          <CopyableRunId value={run.workflow_run_id} />
        </div>
      )}
    </div>
  );
}
