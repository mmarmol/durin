import { useTranslation } from "react-i18next";

import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { NodeDetailBody } from "@/components/workflows/NodeDetailBody";
import { useNodeTranscript } from "@/hooks/useNodeTranscript";
import type { WorkflowRunNode } from "@/lib/api";
import type { WorkActivity } from "@/lib/types";

export interface NodeDetailSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The manifest row to describe; null renders an empty drawer (closing). */
  row: WorkflowRunNode | null;
  typicalS?: number;
  activity?: WorkActivity | null;
  round?: number | null;
  maxRounds?: number | null;
  /** True while this node is the one in flight — the transcript keeps re-reading. */
  live?: boolean;
}

/**
 * The node panel: one drawer, opened from the executions pane and from the
 * chat's work strip alike. It owns the transcript fetch, so both callers hand it
 * the same thing — a manifest row — and both get the same panel.
 */
export function NodeDetailSheet({
  open,
  onOpenChange,
  row,
  typicalS,
  activity,
  round,
  maxRounds,
  live = false,
}: NodeDetailSheetProps) {
  const { t } = useTranslation();
  const { messages, state } = useNodeTranscript(row?.session_key ?? null, live);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      {/* Wider than the primitive's default: a transcript with tool blocks in a
          narrow drawer wraps into an unreadable column. */}
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>{t("workflows.nodePanel.title")}</SheetTitle>
        </SheetHeader>
        {row && (
          <NodeDetailBody
            row={row}
            typicalS={typicalS}
            activity={activity}
            round={round}
            maxRounds={maxRounds}
            transcript={messages}
            transcriptState={state}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}
