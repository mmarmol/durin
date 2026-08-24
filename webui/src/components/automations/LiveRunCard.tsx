import { useEffect, useState } from "react";
import { Check, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { causeIcon } from "@/components/automations/RunHistory";
import { getWorkflowRunManifest, type AutomationRun, type WorkflowRunNode, type WorkflowRunResult } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { formatElapsed, useTicker } from "@/lib/work-format";
import { toWorkNodes } from "@/hooks/useWorkState";
import type { InboundEvent, WorkNode } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

/** One row per node_id, taking that node's most recent recorded pass — a
 *  manifest's `runs` is one entry per PASS (a loop node visited 3 times has 3
 *  entries), but this card shows one row per workflow node, like the live
 *  feed does. Unlike the live feed, the manifest has no record of a node that
 *  has not run at all, so this fallback can never produce a "pending" row —
 *  only done/failed (from `runs`) and running (from `active_node`, while
 *  present). That gap is inherent to what the manifest records, not a bug:
 *  the live subscription above is the only source with the full node list.
 */
function nodesFromManifest(manifest: WorkflowRunResult): WorkNode[] {
  const byNode = new Map<string, WorkflowRunNode>();
  for (const r of manifest.runs) byNode.set(r.node_id, r);
  const nodes: WorkNode[] = Array.from(byNode.values()).map((r) => ({
    id: r.node_id,
    status: r.status === "node_failed" || r.status === "persist_failed" ? "failed" : "done",
    ...(r.duration_s != null ? { durationS: r.duration_s } : {}),
  }));
  if (manifest.active_node) {
    nodes.push({
      id: manifest.active_node.node_id,
      label: manifest.active_node.label,
      status: "running",
      startedAt: manifest.active_node.started_at,
    });
  }
  return nodes;
}

// Status icon vocabulary matches WorkItemCard.tsx's NodeStatusIcon (the
// chat work panel's own node row) — this card renders "the same live
// progress of the chat's work panel, not a new view" per the mockup's own
// note, so the vocabulary is restated here rather than imported: WorkItemCard
// bundles its own header (task/label + run-level status), which would
// duplicate this card's cause line above it.
function NodeStatusIcon({ status }: { status: WorkNode["status"] }) {
  if (status === "done") return <Check className="h-3 w-3 shrink-0 text-emerald-600" aria-hidden />;
  if (status === "failed") return <X className="h-3 w-3 shrink-0 text-destructive" aria-hidden />;
  if (status === "running") return <Loader2 className="h-3 w-3 shrink-0 animate-spin text-amber-600" aria-hidden />;
  // pending — the mockup's own glyph ("○"), not WorkItemCard's middot.
  return <span className="w-3 shrink-0 text-center text-muted-foreground/50" aria-hidden>○</span>;
}

// Mirrors WorkItemCard.tsx's nodeElapsed: a live clock for the running node,
// the recorded span for a finished one, nothing for pending.
function nodeElapsed(node: WorkNode, now: number): string | null {
  if (node.status === "running" && node.startedAt != null) {
    return formatElapsed(node.startedAt * 1000, now);
  }
  if ((node.status === "done" || node.status === "failed") && node.durationS != null) {
    return formatElapsed(0, node.durationS * 1000);
  }
  return null;
}

function LiveNodeRow({ node, now, typicalS }: { node: WorkNode; now: number; typicalS?: number }) {
  const { t } = useTranslation();
  const elapsed = nodeElapsed(node, now);
  // The mockup's own format for the running node only ("0m 48s / típico
  // 1m 30s") — done/pending rows keep the plain elapsed/nothing they already
  // had. Reuses workflows.nodeTypical ("typical"/"típico"), the same word
  // RunNodeRow already uses for the identical concept in the manifest's own
  // per-pass history table, rather than a second translation for one word.
  const display =
    elapsed != null && node.status === "running" && typicalS != null
      ? `${elapsed} / ${t("workflows.nodeTypical")} ${formatElapsed(0, typicalS * 1000)}`
      : elapsed;
  return (
    <div className="flex items-center gap-2 border-t border-border/60 px-3 py-1.5 text-[12px] first:border-t-0">
      <NodeStatusIcon status={node.status} />
      <span
        className={cn(
          "min-w-0 flex-1 truncate",
          node.status === "running" && "font-medium text-accent-foreground",
          node.status === "pending" && "text-muted-foreground",
          node.status === "failed" && "text-destructive",
        )}
      >
        {node.label ?? node.id}
      </span>
      {display != null && (
        <span className="shrink-0 tabular-nums text-[11px] text-muted-foreground">{display}</span>
      )}
    </div>
  );
}

/** One running run of an automation: its cause and the workflow's live,
 *  node-by-node progress — mockup screen 3. Live source is the `runs:feed`
 *  websocket key every service-path run publishes onto (A5); the manifest
 *  poll is both the fallback while no live frame has arrived yet and the
 *  terminal source of truth once the run finishes. No "Detener" control:
 *  neither `durin/service/workflows.py` nor `durin/service/tasks.py` expose a
 *  cancel Command for an API-launched run (only the internal
 *  `durin.workflow.cancellation` flags the engine itself checks), and
 *  RunsView.tsx — the executions screen's own live-run card — renders no
 *  stop button either. Inventing an endpoint here was out of scope; the gap
 *  is disclosed rather than silently worked around. */
export function LiveRunCard({ run, workflow }: { run: AutomationRun; workflow: string }) {
  const { t } = useTranslation();
  const { client, token } = useClient();
  const [liveNodes, setLiveNodes] = useState<WorkNode[] | null>(null);
  const [manifest, setManifest] = useState<WorkflowRunResult | null>(null);

  const runId = run.workflow_run_id;

  // Live subscription: the reserved global "runs:feed" key every service-path
  // run's progress publishes onto (durin/service/workflows.py's
  // RUNS_FEED_CHAT_ID), filtered down to just this run's frames by call_id —
  // the same key carries every OTHER running automation's frames too. Reuses
  // useWorkState's own toWorkNodes parser rather than re-deriving the
  // WorkNode mapping for the identical wire shape.
  useEffect(() => {
    if (!runId) return;
    const expectedCallId = `workflow:${runId}`;
    const handle = (ev: InboundEvent) => {
      if (ev.event !== "message") return;
      if (ev.kind !== "tool_hint" && ev.kind !== "progress") return;
      if (!Array.isArray(ev.tool_events)) return;
      for (const te of ev.tool_events) {
        if (!te || te.name !== "workflow_progress" || te.call_id !== expectedCallId) continue;
        setLiveNodes(toWorkNodes(te.nodes));
      }
    };
    return client.onChat("runs:feed", handle);
  }, [client, runId]);

  // Fallback + terminal truth: poll the manifest every 4s while this run has
  // a workflow_run_id to poll (RunsView.tsx's own anyRunning/setInterval
  // pattern, scoped to one run instead of the whole feed). workflow_run_id is
  // `string | null` on the manifest — start_run initializes it to null and it
  // is only set once the automation runtime actually launches the workflow,
  // so a run between "fired" and "workflow launched" has nothing to poll yet.
  // No stale-id guard needed on the response (unlike RunsView's own poll,
  // which re-targets whichever run the user has since selected): DetailView
  // keys one LiveRunCard per run_id, so this effect's own `runId` closure
  // never points at a different run out from under it — only `cancelled`
  // (StrictMode's double-invoke, or a genuine unmount) needs guarding.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    const load = () => {
      getWorkflowRunManifest(token, workflow, runId)
        .then((m) => {
          if (!cancelled) setManifest(m);
        })
        .catch(() => undefined);
    };
    load();
    const id = setInterval(load, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [token, workflow, runId]);

  // Live frames win once any have arrived; the manifest fills the gap before
  // the first one lands (or if the buffer race described in the runs:feed
  // subscription above hands this run's backlog to a sibling LiveRunCard
  // instead — see the onChat dispatch: only the first subscriber for a given
  // chat_id drains the pending buffer, so a second simultaneous running run
  // can start with nothing live until its next fresh frame).
  const nodes = liveNodes ?? (manifest ? nodesFromManifest(manifest) : null);
  const now = useTicker(nodes != null && nodes.some((n) => n.status === "running"));

  return (
    <div className="rounded-lg border border-border" data-testid="live-run-card">
      <div className="border-b border-border px-3.5 py-2.5 text-[13px] font-semibold">
        {t("automations.detail.live.title")}
      </div>
      <div className="flex items-start gap-2 px-3.5 py-2 text-[12.5px]">
        <span className="shrink-0 text-muted-foreground">{t("automations.detail.causeLabel")}</span>
        <span className="min-w-0 flex-1">
          {causeIcon(run.cause.kind)} {run.cause.excerpt}{" "}
          <span className="text-[11px] text-muted-foreground">· {relativeTime(run.started_at * 1000)}</span>
        </span>
      </div>
      {nodes != null && nodes.length > 0 && (
        <div className="flex flex-col">
          {nodes.map((node) => (
            <LiveNodeRow key={node.id} node={node} now={now} typicalS={manifest?.typical_s?.[node.id]} />
          ))}
        </div>
      )}
    </div>
  );
}
