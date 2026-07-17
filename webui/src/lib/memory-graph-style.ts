import type { MemoryGraphNode } from "@/lib/api";

// Shared visual + browse logic for the memory Entities views (graph canvas,
// cards grid, table). Lives outside MemoryGraphView so the three
// presentations style and filter nodes identically.

export const TYPE_PALETTE: Record<string, string> = {
  person: "#7C3AED",
  project: "#0EA5E9",
  topic: "#10B981",
  place: "#F59E0B",
  event: "#EF4444",
  artifact: "#8B5CF6",
  stance: "#EC4899",
  practice: "#14B8A6",
  // Sessions are deliberately grey-ish so they read as scaffolding
  // around the semantic entities, not as entities themselves.
  session: "#64748B",
  // References (ingested source documents) — amber, distinct from `place`.
  reference: "#D97706",
};

const FALLBACK_HUES = [200, 25, 145, 285, 60, 320, 95];

export function colorForType(type: string): string {
  if (TYPE_PALETTE[type]) return TYPE_PALETTE[type];
  let h = 0;
  for (let i = 0; i < type.length; i++) h = (h * 31 + type.charCodeAt(i)) >>> 0;
  const hue = FALLBACK_HUES[h % FALLBACK_HUES.length];
  return `hsl(${hue} 65% 55%)`;
}

export type EntitySortKey = "recent" | "mentions" | "name";

export interface BrowseOptions {
  hiddenTypes: Set<string>;
  query: string;
  sortKey: EntitySortKey;
}

// Node kinds that are graph scaffolding, not consultable entities: sessions
// have their own detail surface reached via the graph, and references live in
// the Documents tab. Neither belongs in the cards/table inventory.
const NON_ENTITY_TYPES = new Set(["session", "reference"]);

function matchesQuery(node: MemoryGraphNode, q: string): boolean {
  if (!q) return true;
  if (node.name.toLowerCase().includes(q)) return true;
  if (node.aliases.some((a) => a.toLowerCase().includes(q))) return true;
  if ((node.summary ?? "").toLowerCase().includes(q)) return true;
  return false;
}

/** Filter + sort the graph payload's nodes for the cards/table views.
 *  Applies the same type/phantom toggles as the graph canvas, plus the
 *  live query filter (name, aliases, summary substring). */
export function browseEntities(
  nodes: MemoryGraphNode[],
  { hiddenTypes, query, sortKey }: BrowseOptions,
): MemoryGraphNode[] {
  const q = query.trim().toLowerCase();
  const out = nodes.filter((n) => {
    if (NON_ENTITY_TYPES.has(n.type)) return false;
    if (hiddenTypes.has(n.type)) return false;
    if (n.phantom && hiddenTypes.has("phantom")) return false;
    return matchesQuery(n, q);
  });
  const byName = (a: MemoryGraphNode, b: MemoryGraphNode) =>
    a.name.localeCompare(b.name);
  if (sortKey === "name") {
    out.sort(byName);
  } else if (sortKey === "mentions") {
    out.sort((a, b) => b.weight - a.weight || byName(a, b));
  } else {
    // "recent": newest updated_at first; nodes without a timestamp
    // (phantoms, never-updated pages) sink below, ordered by weight.
    out.sort((a, b) => {
      const ta = a.updated_at ?? "";
      const tb = b.updated_at ?? "";
      if (ta !== tb) return tb.localeCompare(ta);
      return b.weight - a.weight || byName(a, b);
    });
  }
  return out;
}
