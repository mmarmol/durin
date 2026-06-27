import { Check, GitBranch, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type { WorkBranch, WorkItem, WorkNode } from "@/lib/types";

// Status icon vocabulary matches ToolBlocks.tsx: spinner for running,
// check for done, X for failed, muted dot for pending.
function NodeStatusIcon({ status }: { status: WorkNode["status"] }) {
  if (status === "done") return <Check className="h-3 w-3 text-emerald-600" aria-hidden />;
  if (status === "failed") return <X className="h-3 w-3 text-destructive" aria-hidden />;
  if (status === "running") return <Loader2 className="h-3 w-3 animate-spin text-amber-600" aria-hidden />;
  // pending
  return <span className="h-3 w-3 flex items-center justify-center text-muted-foreground/50" aria-hidden>·</span>;
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
  // needs_input — a distinct marker; reuses amber but shows a static pause glyph.
  return <span className="h-3.5 w-3.5 flex items-center justify-center text-amber-500 text-[11px] font-bold" aria-hidden>?</span>;
}

/** Presentational card for one WorkItem (workflow or sub-agent). */
export function WorkItemCard({ item }: { item: WorkItem }): JSX.Element {
  const { t } = useTranslation();

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
          <span className="text-[11px] text-amber-500">
            {t("tasks.status.needs_input")}
          </span>
        )}
        <ItemStatusIcon status={item.status} />
      </div>

      {/* Workflow: node list with optional nested parallel branches */}
      {item.kind === "workflow" && item.nodes && item.nodes.length > 0 && (
        <ul className="mt-1.5 flex flex-col gap-0.5 pl-5">
          {item.nodes.map((node, ni) => (
            <li key={`${node.id}-${ni}`}>
              <div className="flex items-center gap-2 text-[12.5px]">
                <NodeStatusIcon status={node.status} />
                <span
                  className={cn(
                    "text-foreground/80",
                    node.status === "failed" && "text-destructive",
                    node.status === "pending" && "text-muted-foreground/60",
                  )}
                >
                  {node.id}
                </span>
              </div>
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
                        {branch.id}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
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
