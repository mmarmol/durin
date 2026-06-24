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
  [k: string]: unknown;
};

export type WorkflowDef = {
  name: string;
  start: string;
  nodes: WorkflowNodeDef[];
  max_visits?: number;
  improvement_mode?: string;
};

export type FlowNodeData = { node: WorkflowNodeDef; isStart: boolean };

const COL = 240;
const ROW = 110;

function targetsOf(n: WorkflowNodeDef): string[] {
  const out: string[] = [];
  for (const t of [n.next, n.on_pass, n.on_fail, ...(n.branches ?? [])]) {
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
  for (const n of def.nodes) {
    if (nodeRoutes(n)) {
      // Routing node (kind:"work" with on_pass/on_fail OR legacy kind:"decision")
      add(n.id, n.on_pass, "pass");
      add(n.id, n.on_fail, "fail");
    } else if (n.kind === "parallel") {
      for (const b of n.branches ?? []) add(n.id, b, "branch");
      add(n.id, n.next);
    } else {
      add(n.id, n.next);
    }
  }
  return { nodes, edges };
}
