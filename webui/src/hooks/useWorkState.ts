import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { listBackgroundTasks } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import type { InboundEvent, ToolProgressEvent, WorkBranch, WorkItem, WorkNode } from "@/lib/types";

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Map a raw node array (from either live WS frames or polled BackgroundTask)
 * into WorkNode[]. Nodes with null/absent branches map to WorkNode with no
 * branches property.
 */
function toWorkNodes(
  raw: Array<{
    id: string;
    label?: string;
    status: string;
    iteration?: number | null;
    budget?: number | null;
    branches?: Array<{ id: string; label?: string; status: string }> | null;
  }> | null | undefined,
): WorkNode[] {
  if (!raw) return [];
  return raw.map((n) => {
    const branches: WorkBranch[] | undefined = n.branches?.map((b) => ({
      id: b.id,
      ...(b.label ? { label: b.label } : {}),
      status: b.status as WorkBranch["status"],
    }));
    return {
      id: n.id,
      ...(n.label ? { label: n.label } : {}),
      status: n.status as WorkNode["status"],
      ...(n.iteration != null ? { iteration: n.iteration } : {}),
      ...(n.budget != null ? { budget: n.budget } : {}),
      ...(branches && branches.length > 0 ? { branches } : {}),
    };
  });
}

/**
 * Parse a `workflow_progress` tool event into a WorkItem.
 * The call_id convention is "workflow:<run_id>".
 */
function workItemFromWorkflowEvent(
  ev: ToolProgressEvent,
  now: number,
): WorkItem | null {
  const e = ev as {
    call_id?: string;
    phase?: string;
    arguments?: unknown;
    nodes?: Array<{
      id: string;
      label?: string;
      status: "running" | "done" | "failed";
      route_label?: string | null;
      iteration?: number | null;
      budget?: number | null;
      branches?: Array<{ id: string; label?: string; status: "running" | "done" | "failed" }>;
    }>;
  };

  const runId = e.call_id?.replace(/^workflow:/, "");
  if (!runId) return null;

  const args = e.arguments as { workflow?: string; task?: string } | undefined;
  const label = args?.workflow ?? runId;
  const task = args?.task ?? undefined;

  const nodes: WorkNode[] = toWorkNodes(e.nodes);

  return {
    kind: "workflow",
    id: runId,
    label,
    ...(task !== undefined ? { task } : {}),
    status: e.phase === "end" ? "done" : "running",
    nodes,
    startedAt: now,
    endedAt: e.phase === "end" ? now : null,
  };
}

/**
 * Parse a `subagent_result` tool event into a WorkItem.
 * The call_id convention is "subagent:<task_id>".
 */
function workItemFromSubagentEvent(
  ev: ToolProgressEvent,
  now: number,
): WorkItem | null {
  const e = ev as {
    call_id?: string;
    phase?: string;
    arguments?: unknown;
    progress?: { iteration?: number; tool?: string | null };
  };

  const taskId = e.call_id?.replace(/^subagent:/, "");
  if (!taskId) return null;

  const args = e.arguments as { label?: string } | undefined;
  const label = args?.label ?? taskId;

  return {
    kind: "subagent",
    id: taskId,
    label,
    status: e.phase === "end" ? "done" : "running",
    steps: e.progress?.iteration,
    startedAt: now,
    endedAt: e.phase === "end" ? now : null,
  };
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Merge live WebSocket progress frames with polled /api/v1/tasks history into
 * a single work list with nested sub-step trees.
 *
 * Live items (from `client.onChat`) win over polled history for the same id —
 * a running item's live tree is fresher than the 4-second poll — EXCEPT when
 * the poll reports the run as decided (done / failed / cancelled). A crashed
 * or externally-reconciled run never emits a final WS frame, so its last live
 * frame says "running" forever; the decided manifest is authoritative there.
 */
export function useWorkState(
  chatId: string | null,
  sessionKey: string | null,
): { active: WorkItem[]; finished: WorkItem[]; refresh: () => void } {
  const { client, token } = useClient();

  // Live items keyed by id — updated from WebSocket frames.
  const liveRef = useRef<Map<string, WorkItem>>(new Map());
  // Polled history — items that arrived from listBackgroundTasks.
  const [polled, setPolled] = useState<WorkItem[]>([]);
  // Incrementing counter to force a re-render when the live map changes.
  const [liveVersion, setLiveVersion] = useState(0);
  // Incrementing counter that triggers an immediate poll when bumped.
  const [pollTrigger, setPollTrigger] = useState(0);

  // Subscribe to live WebSocket events for this chatId.
  useEffect(() => {
    if (!chatId) return;

    const handle = (ev: InboundEvent) => {
      if (ev.event !== "message") return;
      if (ev.kind !== "tool_hint" && ev.kind !== "progress") return;
      if (!Array.isArray(ev.tool_events) || ev.tool_events.length === 0) return;

      const now = Date.now();
      let changed = false;

      for (const te of ev.tool_events) {
        if (!te || typeof te !== "object") continue;
        const name = (te as { name?: string }).name;

        if (name === "workflow_progress") {
          const item = workItemFromWorkflowEvent(te, now);
          if (item) {
            // Preserve startedAt from an earlier frame for the same run.
            const prev = liveRef.current.get(item.id);
            liveRef.current.set(item.id, {
              ...item,
              startedAt: prev?.startedAt ?? item.startedAt,
            });
            changed = true;
          }
        }

        if (name === "subagent_result") {
          const item = workItemFromSubagentEvent(te, now);
          if (item) {
            const prev = liveRef.current.get(item.id);
            liveRef.current.set(item.id, {
              ...item,
              startedAt: prev?.startedAt ?? item.startedAt,
            });
            changed = true;
          }
        }
      }

      if (changed) {
        // Bump version to re-render consumers that read from liveRef.
        setLiveVersion((v) => v + 1);
      }
    };

    const unsub = client.onChat(chatId, handle);
    return () => {
      unsub();
      // Clear live items when switching to a different chat so the new session
      // does not briefly show the previous chat's workflow items before the poll
      // result arrives. Also clear polled state so finished items from the
      // previous session do not flash until the first poll for the new session
      // resolves.
      liveRef.current = new Map();
      setLiveVersion((v) => v + 1);
      setPolled([]);
    };
  }, [chatId, client]);

  // Poll /api/v1/tasks on mount + every 4 s + on explicit refresh().
  useEffect(() => {
    if (!sessionKey) return;

    let cancelled = false;

    const load = () => {
      listBackgroundTasks(token, sessionKey)
        .then((rows) => {
          if (cancelled) return;
          const items: WorkItem[] = rows.map((r) => ({
            kind: r.kind,
            id: r.id,
            label: r.label,
            status: r.status,
            ...(r.task != null ? { task: r.task } : {}),
            ...(r.needs_input_detail != null ? { needsInputDetail: r.needs_input_detail } : {}),
            startedAt: r.started_at,
            endedAt: r.ended_at,
            nodes: toWorkNodes(r.nodes),
          }));
          setPolled(items);
        })
        .catch(() => {
          if (!cancelled) setPolled([]);
        });
    };

    load();
    const id = setInterval(load, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  // pollTrigger is intentionally included: bumping it causes an immediate re-run.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, sessionKey, pollTrigger]);

  // Merge: live items win for any id present in both, decided manifests aside.
  // useMemo so the merged array is computed once per render, not called separately.
  // liveVersion triggers re-computation when the live map changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const all = useMemo((): WorkItem[] => {
    const live = liveRef.current;
    const byId = new Map<string, WorkItem>();

    // Seed with polled history.
    for (const item of polled) {
      byId.set(item.id, item);
    }

    // Live items override polled history — unless the manifest already DECIDED
    // the run (done / failed / cancelled). A crashed or reconciled run emits no
    // final WS frame, so its last live frame stays "running" forever; without
    // this guard that stale frame pins the panel and strip to in-progress. A
    // polled needs_input does NOT count as decided: a resumed run's fresh
    // running frame must flip the item back to running immediately (the
    // manifest lags the live stream there).
    for (const [id, item] of live) {
      const polledItem = byId.get(id);
      const decided =
        polledItem != null &&
        polledItem.status !== "running" &&
        polledItem.status !== "needs_input";
      if (!decided) byId.set(id, item);
    }

    return Array.from(byId.values());
  }, [polled, liveVersion]);
  const active = all.filter(
    (w) => w.status === "running" || w.status === "needs_input",
  );
  const finished = all
    .filter((w) => w.status !== "running" && w.status !== "needs_input")
    .sort((a, b) => (b.endedAt ?? b.startedAt) - (a.endedAt ?? a.startedAt));

  const refresh = useCallback(() => {
    setPollTrigger((v) => v + 1);
  }, []);

  return { active, finished, refresh };
}
