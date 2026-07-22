import { Check, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useThreadActions } from "@/components/thread/ThreadActionsContext";
import { cn } from "@/lib/utils";
import { formatElapsed, useTicker } from "@/lib/work-format";
import type { ToolProgressEvent } from "@/lib/types";

/**
 * Compact pill shown in the chat thread for `workflow_progress` and
 * `subagent_result` events. Replaces the old full inline blocks — all detail
 * lives in the side work panel. Clicking opens that panel via
 * `ThreadActions.openWorkPanel`.
 *
 * Running state shows a spinning loader; ended/done state shows a check mark.
 * The label is the workflow name (from `arguments.workflow`) for workflow events,
 * or the sub-agent label (from `arguments.label`) for subagent events.
 *
 * While running and the event carries a node list, the label is followed by
 * the in-flight node's own label and a live elapsed clock (`· Consolidate ·
 * 4:21`) so the pill answers "what is it doing right now" without opening the
 * panel. Silent when the node list is absent or carries no timing yet.
 */
export function WorkChip({ event }: { event: ToolProgressEvent }) {
  const { t } = useTranslation();
  const actions = useThreadActions();

  const a = (event.arguments ?? {}) as Record<string, unknown>;
  const label =
    typeof a.workflow === "string"
      ? a.workflow
      : typeof a.label === "string"
        ? a.label
        : event.name ?? "";

  const running = event.phase === "running";
  // The node currently in flight, if the event carries a node list — undefined
  // once the run ends, or for subagent_result events (which carry no nodes).
  const node = running
    ? event.nodes?.filter((n) => n.status === "running").at(-1)
    : undefined;
  const now = useTicker(node?.started_at != null);

  return (
    <button
      type="button"
      title={t("message.workChip.openPanel")}
      aria-label={t("message.workChip.openPanel")}
      onClick={() => actions?.openWorkPanel?.()}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-border/60",
        "bg-muted/30 px-2.5 py-0.5 text-[11.5px] text-muted-foreground",
        "hover:bg-muted/60 hover:text-foreground transition-colors cursor-pointer",
      )}
    >
      {running ? (
        <Loader2 className="h-3 w-3 animate-spin shrink-0" aria-hidden />
      ) : (
        <Check className="h-3 w-3 shrink-0 text-emerald-500" aria-hidden />
      )}
      <span>
        {label}
        {node && node.started_at != null && (
          <>
            {" "}
            · {node.label ?? node.id} · {formatElapsed(node.started_at * 1000, now)}
          </>
        )}
      </span>
    </button>
  );
}
