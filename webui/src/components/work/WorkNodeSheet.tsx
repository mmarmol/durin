import { useEffect, useState } from "react";

import { NodeDetailSheet } from "@/components/workflows/NodeDetailSheet";
import { getWorkflowRunManifest, type WorkflowRunNode } from "@/lib/api";
import type { WorkItem, WorkNode } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

/**
 * The chat strip's door into the node panel.
 *
 * A strip frame is not a manifest row: it carries no session key, no captured
 * script output, no artifacts. This resolves the row from the run's manifest and
 * hands it to the same drawer the executions pane opens, so both surfaces show
 * the same panel rather than two lookalikes.
 */
export function WorkNodeSheet({
  opened,
  onClose,
}: {
  /** The node the user asked to open, with the item it belongs to; null = closed. */
  opened: { item: WorkItem; node: WorkNode } | null;
  onClose: () => void;
}): JSX.Element {
  const { token } = useClient();
  const [row, setRow] = useState<WorkflowRunNode | null>(null);

  useEffect(() => {
    const workflow = opened?.item.workflow;
    if (!opened || !workflow || !token) {
      setRow(null);
      return;
    }
    let cancelled = false;
    void getWorkflowRunManifest(token, workflow, opened.item.id)
      .then((manifest) => {
        if (cancelled) return;
        // The LAST pass for this node id: after a loop-back the strip shows the
        // node's current pass, and an earlier row would be a different turn.
        const rows = manifest.runs.filter((r) => r.node_id === opened.node.id);
        const active = manifest.active_node;
        if (rows.length > 0) {
          setRow(rows[rows.length - 1]);
        } else if (active?.node_id === opened.node.id && active.session_key) {
          // Still in flight: no completed row yet, but the manifest advertises
          // the session the node is writing.
          setRow({
            node_id: active.node_id,
            iteration: active.iteration ?? 1,
            status: "ok",
            passed: null,
            route_label: null,
            session_key: active.session_key,
            worker_index: null,
          });
        } else {
          setRow(null);
        }
      })
      .catch(() => {
        if (!cancelled) setRow(null);
      });
    return () => {
      cancelled = true;
    };
  }, [opened, token]);

  return (
    <NodeDetailSheet
      open={opened != null}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      row={row}
      activity={opened?.node.activity ?? null}
      round={opened?.node.round ?? null}
      maxRounds={opened?.node.maxRounds ?? null}
      live={opened?.node.status === "running"}
    />
  );
}
