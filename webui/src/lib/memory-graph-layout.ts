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

// Grid cell size (screen px) for label collision culling, keyed by zoom.
// Decreases as the camera zooms in: fewer, more widely-spaced cells at
// zoomed-out scales (where many nodes overlap on screen) and progressively
// smaller cells as zoom gives labels more room to breathe. This replaces a
// global numeric label cap: culling is purely local (one label per cell),
// so a region of same-score nodes never shows an id-ordered arbitrary subset
// — every node not sharing a cell with a higher-priority neighbor gets its
// label.
export function labelCellSize(zoomK: number): number {
  if (zoomK < 0.75) return 150;
  if (zoomK < 1.5) return 115;
  if (zoomK < 2.5) return 90;
  return 70;
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
  cell: number,
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
  for (const c of ordered) {
    const key = `${Math.round(c.sx / cell)}:${Math.round(c.sy / cell)}`;
    if (taken.has(key)) continue;
    taken.add(key);
    out.add(c.id);
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
