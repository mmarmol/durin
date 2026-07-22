import type {
  MemoryGraphNode,
  MemoryGraphPayload,
  MemoryOverviewPayload,
} from "@/lib/api";

export const NODE_RADIUS_MAX = 26;
export const SESSION_RADIUS = 6;
export const BUBBLE_RADIUS_MIN = 18;
export const BUBBLE_RADIUS_MAX = 60;

export type OverviewNode = MemoryGraphNode & { kind?: "bubble"; count?: number };

export function radiusForNode(weight: number, type: string): number {
  if (type === "session") return SESSION_RADIUS;
  const r = 5 + Math.log1p(Math.max(0, weight)) * 3.2;
  return Math.min(NODE_RADIUS_MAX, r);
}

export function radiusForBubble(count: number): number {
  const r = BUBBLE_RADIUS_MIN + Math.log1p(Math.max(0, count)) * 5.5;
  return Math.min(BUBBLE_RADIUS_MAX, r);
}

export function labelBudget(zoomK: number): number {
  if (zoomK < 0.75) return 14;
  if (zoomK < 1.5) return 40;
  if (zoomK < 2.5) return 90;
  return 250;
}

export interface LabelCandidate {
  id: string;
  sx: number;
  sy: number;
  weight: number;
  priority?: boolean;
}

export function visibleLabels(
  cands: LabelCandidate[],
  viewport: { w: number; h: number },
  budget: number,
  cell = 90,
): Set<string> {
  const margin = 40;
  const inView = cands.filter(
    (c) =>
      c.sx >= -margin &&
      c.sx <= viewport.w + margin &&
      c.sy >= -margin &&
      c.sy <= viewport.h + margin,
  );
  const ordered = inView.sort((a, b) => {
    if (!!a.priority !== !!b.priority) return a.priority ? -1 : 1;
    return b.weight - a.weight || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  });
  const taken = new Set<string>();
  const out = new Set<string>();
  let spent = 0;
  for (const c of ordered) {
    const key = `${Math.round(c.sx / cell)}:${Math.round(c.sy / cell)}`;
    if (taken.has(key)) continue;
    if (!c.priority && spent >= budget) continue;
    taken.add(key);
    out.add(c.id);
    if (!c.priority) spent++;
  }
  return out;
}

export function overviewToGraph(o: MemoryOverviewPayload): MemoryGraphPayload {
  const bubbleNodes: OverviewNode[] = o.bubbles.map((b) => ({
    id: b.id,
    type: b.types[0] ?? "topic",
    name: b.name,
    aliases: [],
    weight: b.count,
    kind: "bubble",
    count: b.count,
  }));
  const nodes: OverviewNode[] = [...bubbleNodes, ...o.hubs, ...o.loose];
  return {
    nodes,
    edges: o.edges,
    stats: {
      node_count: nodes.length,
      edge_count: o.edges.length,
      phantom_count: 0,
      truncated_nodes: false,
      truncated_edges: false,
      types: Array.from(
        new Set([...o.hubs, ...o.loose].map((n) => n.type)),
      ).sort(),
    },
  };
}
