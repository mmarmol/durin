// Mapping between a durin workflow definition (the raw on-disk JSON, snake_case keys)
// and a React Flow graph (positioned nodes + edges). The read direction
// (workflowToFlow) renders a saved workflow; positions are auto-laid-out in layers by
// distance from the start node, since the JSON carries no coordinates.

import type { Edge, Node } from "@xyflow/react";

export type WorkflowNodeDef = {
  id: string;
  kind: "work" | "decision" | "parallel" | "subworkflow";
  next?: string | null;
  on_pass?: string | null;
  on_fail?: string | null;
  branches?: string[];
  // parallel dynamic mode fields
  worker?: string | null;
  list_from?: string | null;
  max_concurrency?: number;
  reconcile?: "read" | "choose" | "union";
  [k: string]: unknown;
};

export type IODescriptor = {
  text?: boolean;
  file?: boolean;
};

export type WorkflowDef = {
  name: string;
  start: string;
  nodes: WorkflowNodeDef[];
  max_visits?: number;
  improvement_mode?: string;
  input?: IODescriptor;
  output?: IODescriptor;
};

export type FlowNodeData = { node: WorkflowNodeDef; isStart: boolean };

const COL = 240;
const ROW = 110;

function targetsOf(n: WorkflowNodeDef): string[] {
  const out: string[] = [];
  for (const t of [n.next, n.on_pass, n.on_fail, n.worker, n.list_from, ...(n.branches ?? [])]) {
    if (typeof t === "string") out.push(t);
  }
  return out;
}

// Distance from the start node (BFS). Nodes unreachable from start fall back to 0.
function computeDepths(def: WorkflowDef): Record<string, number> {
  const byId = new Map(def.nodes.map((n) => [n.id, n]));
  const depth: Record<string, number> = {};
  const queue: Array<[string, number]> = [[def.start, 0]];
  while (queue.length) {
    const [id, d] = queue.shift()!;
    if (id in depth || !byId.has(id)) continue;
    depth[id] = d;
    for (const t of targetsOf(byId.get(id)!)) {
      if (!(t in depth)) queue.push([t, d + 1]);
    }
  }
  for (const n of def.nodes) if (!(n.id in depth)) depth[n.id] = 0;
  return depth;
}

// A node "routes" when it has on_pass or on_fail set, regardless of kind.
// This means both legacy kind:"decision" nodes and new kind:"work" nodes with
// routing fields render identically (pass/fail edges + decision ring/handles).
function nodeRoutes(n: WorkflowNodeDef): boolean {
  return n.on_pass != null || n.on_fail != null;
}

// Resolve the React Flow node type: routing nodes always render as "decision"
// so NodeCard shows the decision ring regardless of the stored kind.
function resolveNodeType(n: WorkflowNodeDef): string {
  if (nodeRoutes(n)) return "decision";
  return n.kind;
}

// Find terminal nodes: nodes reachable from start that have no valid outgoing targets.
// A dynamic parallel's worker node is excluded — it has no `next` by design (it hands
// off via text to the merge node), so including it would draw a spurious worker→__output__ edge.
function findTerminals(def: WorkflowDef, byId: Map<string, WorkflowNodeDef>): string[] {
  const depth = computeDepths(def);
  const reachable = new Set(Object.keys(depth));
  const dynamicWorkers = new Set<string>();
  for (const n of def.nodes) {
    if (n.kind === "parallel" && typeof n.worker === "string") dynamicWorkers.add(n.worker);
  }
  return def.nodes
    .filter(
      (n) =>
        reachable.has(n.id) &&
        !dynamicWorkers.has(n.id) &&
        targetsOf(n).filter((t) => byId.has(t)).length === 0,
    )
    .map((n) => n.id);
}

export function workflowToFlow(def: WorkflowDef): { nodes: Node[]; edges: Edge[] } {
  const byId = new Map(def.nodes.map((n) => [n.id, n]));
  const depth = computeDepths(def);
  const rowByCol: Record<number, number> = {};

  const nodes: Node[] = def.nodes.map((n) => {
    const col = depth[n.id] ?? 0;
    const row = (rowByCol[col] = (rowByCol[col] ?? 0) + 1) - 1;
    return {
      id: n.id,
      type: resolveNodeType(n),
      position: { x: col * COL, y: row * ROW },
      data: { node: n, isStart: n.id === def.start } satisfies FlowNodeData,
    };
  });

  const edges: Edge[] = [];
  const add = (source: string, to: unknown, label?: string) => {
    if (typeof to === "string" && byId.has(to)) {
      edges.push({ id: `${source}->${to}:${label ?? ""}`, source, target: to, label });
    }
  };

  // Track which nodes are dynamic-parallel workers so they can carry the ×N badge.
  const dynamicWorkerIds = new Set<string>();
  for (const n of def.nodes) {
    if (n.kind === "parallel" && n.worker) dynamicWorkerIds.add(n.worker);
  }

  for (const n of def.nodes) {
    if (nodeRoutes(n)) {
      // Routing node (kind:"work" with on_pass/on_fail OR legacy kind:"decision")
      add(n.id, n.on_pass, "pass");
      add(n.id, n.on_fail, "fail");
    } else if (n.kind === "parallel") {
      const isDynamic = typeof n.worker === "string";
      if (isDynamic) {
        // Dynamic parallel: list_from → parallel (edge from list source), parallel → worker, parallel → next
        add(n.id, n.list_from, "list");
        add(n.id, n.worker, "worker");
        add(n.id, n.next);
      } else {
        // Static parallel: fan out to branches, merge to next
        for (const b of n.branches ?? []) add(n.id, b, "branch");
        add(n.id, n.next);
      }
    } else {
      add(n.id, n.next);
    }
  }

  // Annotate dynamic worker nodes with a marker in their data.
  for (const n of nodes) {
    if (dynamicWorkerIds.has(n.id)) {
      n.data = { ...n.data, dynamicWorker: true };
    }
  }

  // Emit I/O object nodes when the def declares input/output descriptors.
  if (def.input) {
    const inputId = "__input__";
    const startDepth = depth[def.start] ?? 0;
    const inputRow = (rowByCol[-1] = (rowByCol[-1] ?? 0) + 1) - 1;
    nodes.push({
      id: inputId,
      type: "input_obj",
      position: { x: (startDepth - 1) * COL, y: inputRow * ROW },
      data: { input: def.input },
    });
    edges.push({ id: `${inputId}->${def.start}:`, source: inputId, target: def.start });
  }

  if (def.output) {
    const outputId = "__output__";
    const terminals = findTerminals(def, byId);
    // Position after the deepest terminal
    const maxDepth = Math.max(0, ...terminals.map((id) => depth[id] ?? 0));
    const outputRow = (rowByCol[maxDepth + 1] = (rowByCol[maxDepth + 1] ?? 0) + 1) - 1;
    nodes.push({
      id: outputId,
      type: "output_obj",
      position: { x: (maxDepth + 1) * COL, y: outputRow * ROW },
      data: { output: def.output },
    });
    for (const tid of terminals) {
      edges.push({ id: `${tid}->${outputId}:`, source: tid, target: outputId });
    }
  }

  return { nodes, edges };
}
