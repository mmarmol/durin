import { Check, GitBranch, HelpCircle, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { activeNode, formatElapsed, touchedNodeCount, useTicker } from "@/lib/work-format";
import type { WorkBranch, WorkItem, WorkNode } from "@/lib/types";
import { NodeActivityLine } from "./NodeActivityLine";

// Status icon vocabulary matches ToolBlocks.tsx: spinner for running,
// check for done, X for failed, muted dot for pending.
function NodeStatusIcon({ status }: { status: WorkNode["status"] }) {
  if (status === "done") return <Check className="h-3 w-3 text-emerald-600" aria-hidden />;
  if (status === "failed") return <X className="h-3 w-3 text-destructive" aria-hidden />;
  if (status === "running") return <Loader2 className="h-3 w-3 animate-spin text-amber-600" aria-hidden />;
  // pending
  return <span className="h-3 w-3 flex items-center justify-center text-muted-foreground/50" aria-hidden>·</span>;
}

// "pass N of budget" chip for a looping node, shown only once it has entered a
// second (or later) pass — a first pass carries no useful loop information.
function PassChip({ iteration, budget }: { iteration?: number; budget?: number }) {
  const { t } = useTranslation();
  if (budget == null || iteration == null || iteration <= 1) return null;
  return (
    <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
      {t("workflows.passOf", { iteration, budget })}
    </span>
  );
}

// Elapsed shown on the right of a node row: a live clock derived from the
// node's own start instant while running, or its recorded duration once
// finished. A duration is already a span of seconds, not a timestamp — it is
// formatted through the same helper but anchored at 0, never treated as a
// start instant to diff against "now". Absent for pending nodes and for
// older records that predate these fields (additive only).
function nodeElapsed(node: WorkNode, now: number): string | null {
  if (node.status === "running" && node.startedAt != null) {
    return formatElapsed(node.startedAt * 1000, now);
  }
  if ((node.status === "done" || node.status === "failed") && node.durationS != null) {
    return formatElapsed(0, node.durationS * 1000);
  }
  return null;
}

function BranchStatusIcon({ status }: { status: WorkBranch["status"] }) {
  if (status === "done") return <Check className="h-3 w-3 text-emerald-600" aria-hidden />;
  if (status === "failed") return <X className="h-3 w-3 text-destructive" aria-hidden />;
  return <Loader2 className="h-3 w-3 animate-spin text-amber-600" aria-hidden />;
}

// Header-level status indicator for the WorkItem as a whole.
function ItemStatusIcon({ status }: { status: WorkItem["status"] }) {
  if (status === "done") return <Check className="h-3.5 w-3.5 text-emerald-600" aria-hidden />;
  if (status === "failed") return <X className="h-3.5 w-3.5 text-destructive" aria-hidden />;
  if (status === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-600" aria-hidden />;
  // needs_input — accent-tinted: the run paused, it did not fail.
  return <HelpCircle className="h-3.5 w-3.5 text-accent-foreground" aria-hidden />;
}

/** Presentational card for one WorkItem (workflow or sub-agent). */
export function WorkItemCard({ item }: { item: WorkItem }): JSX.Element {
  const { t } = useTranslation();
  // One ticker drives every live clock in this card; frozen (no re-render)
  // once the item is no longer running, so a finished node's clock never ticks.
  const now = useTicker(item.status === "running");
  // The single node the round/activity detail attaches to — shared with the chat
  // strip's own lookup rather than re-derived here, so the panel and the strip
  // always agree on which node is "the" running one.
  const runningNode = activeNode(item);

  return (
    <div className="w-full rounded-lg border border-border/60 bg-muted/25 px-3 py-2">
      {/* Header: task (or label fallback) as title + workflow name tag + status icon */}
      <div className="flex items-center gap-2">
        <GitBranch className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <div className="min-w-0 flex-1">
          {item.kind === "workflow" && item.task ? (
            <>
              <div className="text-[11px] text-muted-foreground leading-tight">{item.label}</div>
              <div className="truncate text-[13px] font-medium text-foreground leading-snug">
                {item.task}
              </div>
            </>
          ) : (
            <span className="truncate text-[13px] font-medium text-foreground">{item.label}</span>
          )}
        </div>
        {item.status === "needs_input" && (
          <span className="rounded bg-accent px-1.5 py-0.5 text-[11px] text-accent-foreground">
            {t("tasks.status.needs_input")}
          </span>
        )}
        <ItemStatusIcon status={item.status} />
      </div>

      {/* needs_input: neutral hand-off copy — the calling agent owns the resume,
          not this card, so there is no resume form here. */}
      {item.status === "needs_input" && (
        <div className="mt-1 pl-5 text-[11px] text-muted-foreground">
          {t("tasks.needsInputHint")}
        </div>
      )}

      {/* The gate's actual questions, when the manifest carried them. */}
      {item.needsInputDetail && (
        <div className="mt-2 ml-5 rounded-md bg-muted px-3 py-2 text-xs">
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("tasks.questions")}
          </div>
          <div className="whitespace-pre-wrap">{item.needsInputDetail}</div>
        </div>
      )}

      {/* Workflow: node list with optional nested parallel branches */}
      {item.kind === "workflow" && item.nodes && item.nodes.length > 0 && (
        <>
          <ul className="mt-1.5 flex flex-col gap-0.5 pl-5">
            {item.nodes.map((node, ni) => {
              const elapsed = nodeElapsed(node, now);
              // The round/activity detail is scoped to the single active node
              // (not merely any node whose status happens to read "running"),
              // and only when it has something to show — no empty indent.
              const showDetail =
                node === runningNode &&
                (node.activity != null || (node.round != null && node.maxRounds != null));
              return (
                <li key={`${node.id}-${ni}`}>
                  {/* A node running inside a sub-workflow is not part of this
                      run's own graph: rail it in, like a parallel branch, so the
                      two are not read as siblings. */}
                  <div
                    className={cn(
                      "flex items-center gap-2 text-[12.5px]",
                      node.parentNode && "ml-3 border-l border-border/50 pl-2",
                    )}
                  >
                    <NodeStatusIcon status={node.status} />
                    <span
                      className={cn(
                        "text-foreground/80",
                        node.status === "failed" && "text-destructive",
                        node.status === "pending" && "text-muted-foreground/60",
                      )}
                      // The node's own sentence, too long for this width, offered
                      // on hover instead of replacing the short label.
                      title={node.description}
                    >
                      {node.label ?? node.id}
                    </span>
                    <PassChip iteration={node.iteration} budget={node.budget} />
                    {elapsed != null && (
                      <span className="ml-auto shrink-0 tabular-nums text-[11px] text-muted-foreground">
                        {elapsed}
                      </span>
                    )}
                  </div>
                  {showDetail && (
                    <div className="mt-0.5 flex flex-col gap-0.5 pl-5">
                      {node.round != null && node.maxRounds != null && (
                        <div className="text-[11px] text-muted-foreground">
                          {t("work.round", { round: node.round, budget: node.maxRounds })}
                        </div>
                      )}
                      {node.activity && <NodeActivityLine activity={node.activity} />}
                    </div>
                  )}
                  {/* Parallel branches: indented beneath the node with a left rail */}
                  {node.branches && node.branches.length > 0 && (
                    <ul className="ml-4 mt-0.5 flex flex-col gap-0.5 border-l border-border/50 pl-3">
                      {node.branches.map((branch, bi) => (
                        <li key={`${branch.id}-${bi}`} className="flex items-center gap-2 text-[12px]">
                          <BranchStatusIcon status={branch.status} />
                          <span
                            className={cn(
                              "text-foreground/70",
                              branch.status === "failed" && "text-destructive",
                            )}
                          >
                            {branch.label ?? branch.id}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
          {/* Footer: nodes touched (never "N of M" — the pending tail is not a
              promise) and the run's total elapsed. */}
          <div className="mt-1 flex items-center gap-1.5 pl-5 text-[11px] text-muted-foreground">
            <span>{t("work.nodeCount", { count: touchedNodeCount(item) })}</span>
            <span aria-hidden>·</span>
            <span className="tabular-nums">
              {formatElapsed(item.startedAt, item.endedAt ?? now)}
            </span>
          </div>
        </>
      )}

      {/* Sub-agent: compact step count */}
      {item.kind === "subagent" && (
        <div className="mt-1 pl-5 text-[12px] text-muted-foreground">
          {t("work.steps", { count: item.steps ?? 0 })}
        </div>
      )}
    </div>
  );
}
