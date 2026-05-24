import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Network, RefreshCw, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useMemoryGraph } from "@/hooks/useMemoryGraph";
import { cn } from "@/lib/utils";
import type { MemoryGraphNode } from "@/lib/api";

interface MemoryGraphViewProps {
  active: boolean;
  onToggleSidebar?: () => void;
  hideSidebarToggleOnDesktop?: boolean;
}

interface SimNode extends MemoryGraphNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
  pinned: boolean;
}

interface SimEdge {
  source: SimNode;
  target: SimNode;
  weight: number;
}

// Stable palette per type — same hues across renders so the legend
// matches the canvas. Types beyond this list cycle through.
const TYPE_PALETTE: Record<string, string> = {
  person: "#7C3AED",   // violet
  project: "#0EA5E9",  // sky
  topic: "#10B981",    // emerald
  place: "#F59E0B",    // amber
  event: "#EF4444",    // red
  artifact: "#8B5CF6", // purple
  stance: "#EC4899",   // pink
  practice: "#14B8A6", // teal
};
const FALLBACK_HUES = [200, 25, 145, 285, 60, 320, 95];

function colorForType(type: string): string {
  if (TYPE_PALETTE[type]) return TYPE_PALETTE[type];
  // Deterministic hue from type string.
  let h = 0;
  for (let i = 0; i < type.length; i++) h = (h * 31 + type.charCodeAt(i)) >>> 0;
  const hue = FALLBACK_HUES[h % FALLBACK_HUES.length];
  return `hsl(${hue} 65% 55%)`;
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** Tiny force-directed layout. Vanilla, no d3 dependency.
 *  - Repulsion: O(N^2) Coulomb between every pair.
 *  - Attraction: spring along each edge.
 *  - Centering: gentle pull toward the canvas centre.
 *  Good enough for ≤200 nodes which is doc 25 §1 budget for typical workspaces. */
function tickForces(
  nodes: SimNode[],
  edges: SimEdge[],
  width: number,
  height: number,
  alpha: number,
) {
  const cx = width / 2;
  const cy = height / 2;

  // Repulsion + centering
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    if (a.pinned) continue;
    let fx = (cx - a.x) * 0.005;
    let fy = (cy - a.y) * 0.005;
    for (let j = 0; j < nodes.length; j++) {
      if (i === j) continue;
      const b = nodes[j];
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const d2 = dx * dx + dy * dy + 0.01;
      const d = Math.sqrt(d2);
      // Repulsion strength scales mildly with size for visual breathing room.
      const sizeBoost = 1 + Math.log(2 + (a.weight ?? 0) + (b.weight ?? 0)) * 0.4;
      const k = (2200 * sizeBoost) / d2;
      fx += (dx / d) * k;
      fy += (dy / d) * k;
    }
    a.vx = (a.vx + fx * alpha) * 0.82;
    a.vy = (a.vy + fy * alpha) * 0.82;
  }

  // Spring attraction along edges
  for (const e of edges) {
    const dx = e.target.x - e.source.x;
    const dy = e.target.y - e.source.y;
    const d = Math.sqrt(dx * dx + dy * dy + 0.01);
    const rest = 90; // desired edge length
    const k = 0.03 * Math.min(4, e.weight); // heavier edges pull harder
    const f = (d - rest) * k;
    const fx = (dx / d) * f * alpha;
    const fy = (dy / d) * f * alpha;
    if (!e.source.pinned) {
      e.source.vx += fx;
      e.source.vy += fy;
    }
    if (!e.target.pinned) {
      e.target.vx -= fx;
      e.target.vy -= fy;
    }
  }

  for (const n of nodes) {
    if (n.pinned) continue;
    n.x = clamp(n.x + n.vx, 20, width - 20);
    n.y = clamp(n.y + n.vy, 20, height - 20);
  }
}

function radiusForWeight(weight: number): number {
  return 5 + Math.sqrt(Math.max(0, weight)) * 2.2;
}

export function MemoryGraphView(props: MemoryGraphViewProps) {
  const { data, loading, error, refresh } = useMemoryGraph(props.active);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const simNodesRef = useRef<SimNode[]>([]);
  const simEdgesRef = useRef<SimEdge[]>([]);
  const alphaRef = useRef(1);
  const rafRef = useRef<number | null>(null);
  const draggingRef = useRef<SimNode | null>(null);
  const hoverRef = useRef<SimNode | null>(null);
  const [selected, setSelected] = useState<MemoryGraphNode | null>(null);
  const [search, setSearch] = useState("");

  // Build the simulation arrays whenever the payload changes.
  const { simNodes, simEdges } = useMemo(() => {
    if (!data) return { simNodes: [] as SimNode[], simEdges: [] as SimEdge[] };
    const w = wrapRef.current?.clientWidth ?? 800;
    const h = wrapRef.current?.clientHeight ?? 600;
    const cx = w / 2;
    const cy = h / 2;
    const sims: SimNode[] = data.nodes.map((n, i) => {
      // Initial positions on a circle so the force algorithm has
      // something to relax from instead of all-stacked-at-center.
      const angle = (i / Math.max(1, data.nodes.length)) * Math.PI * 2;
      const radius = Math.min(w, h) * 0.32;
      return {
        ...n,
        x: cx + Math.cos(angle) * radius,
        y: cy + Math.sin(angle) * radius,
        vx: 0,
        vy: 0,
        pinned: false,
      };
    });
    const byId = new Map(sims.map((n) => [n.id, n] as const));
    const edges: SimEdge[] = [];
    for (const e of data.edges) {
      const s = byId.get(e.source);
      const t = byId.get(e.target);
      if (!s || !t) continue;
      edges.push({ source: s, target: t, weight: e.weight });
    }
    return { simNodes: sims, simEdges: edges };
  }, [data]);

  useEffect(() => {
    simNodesRef.current = simNodes;
    simEdgesRef.current = simEdges;
    alphaRef.current = 1;
  }, [simNodes, simEdges]);

  // RAF render loop — kept lean so the canvas stays responsive even
  // with the chat tab still mounted off-screen behind it.
  useEffect(() => {
    if (!props.active) return;
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let stopped = false;

    function resize() {
      if (!canvas || !wrap) return;
      const dpr = window.devicePixelRatio || 1;
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);

    const needle = search.trim().toLowerCase();

    function frame() {
      if (stopped) return;
      const nodes = simNodesRef.current;
      const edges = simEdgesRef.current;
      if (!ctx || !canvas || !wrap) {
        rafRef.current = requestAnimationFrame(frame);
        return;
      }
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;

      // Anneal: alpha decays so the system settles instead of jittering.
      const alpha = alphaRef.current;
      if (alpha > 0.02) {
        tickForces(nodes, edges, w, h, alpha);
        alphaRef.current = alpha * 0.985;
      }

      // Clear & draw
      ctx.clearRect(0, 0, w, h);

      // Edges first (under nodes)
      ctx.lineCap = "round";
      for (const e of edges) {
        const dim = needle
          ? !(
              e.source.id.toLowerCase().includes(needle) ||
              e.target.id.toLowerCase().includes(needle) ||
              e.source.name.toLowerCase().includes(needle) ||
              e.target.name.toLowerCase().includes(needle)
            )
          : false;
        ctx.strokeStyle = dim
          ? "rgba(120,120,140,0.10)"
          : `rgba(120,120,140,${Math.min(0.55, 0.18 + e.weight * 0.06)})`;
        ctx.lineWidth = Math.min(3, 0.8 + Math.log(1 + e.weight));
        ctx.beginPath();
        ctx.moveTo(e.source.x, e.source.y);
        ctx.lineTo(e.target.x, e.target.y);
        ctx.stroke();
      }

      // Nodes
      for (const n of nodes) {
        const r = radiusForWeight(n.weight);
        const matches = !needle ||
          n.id.toLowerCase().includes(needle) ||
          n.name.toLowerCase().includes(needle) ||
          (n.aliases ?? []).some((a) => a.toLowerCase().includes(needle));
        const fill = colorForType(n.type);
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = matches ? fill : `${fill}33`;
        ctx.fill();
        if (n.phantom) {
          ctx.setLineDash([3, 3]);
          ctx.strokeStyle = matches ? "rgba(0,0,0,0.4)" : "rgba(0,0,0,0.15)";
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.setLineDash([]);
        }
        if (selected?.id === n.id || hoverRef.current?.id === n.id) {
          ctx.beginPath();
          ctx.arc(n.x, n.y, r + 3, 0, Math.PI * 2);
          ctx.strokeStyle = "rgba(0,0,0,0.55)";
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }
      }

      // Labels — only for big-enough nodes, or matching the search.
      ctx.font = "11px ui-sans-serif, system-ui, -apple-system";
      ctx.textBaseline = "top";
      ctx.textAlign = "center";
      for (const n of nodes) {
        const r = radiusForWeight(n.weight);
        const matches = !needle ||
          n.id.toLowerCase().includes(needle) ||
          n.name.toLowerCase().includes(needle);
        const shouldLabel = r > 9 || matches || selected?.id === n.id;
        if (!shouldLabel) continue;
        ctx.fillStyle = "rgba(0,0,0,0.75)";
        ctx.fillText(n.name, n.x, n.y + r + 2);
      }

      rafRef.current = requestAnimationFrame(frame);
    }
    rafRef.current = requestAnimationFrame(frame);

    return () => {
      stopped = true;
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      ro.disconnect();
    };
  }, [props.active, search, selected]);

  // Pointer interactions
  const hitTest = useCallback((x: number, y: number): SimNode | null => {
    const nodes = simNodesRef.current;
    // Reverse so top-rendered hits first; here z is unordered, so use radius gate.
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      const r = radiusForWeight(n.weight) + 4;
      const dx = x - n.x;
      const dy = y - n.y;
      if (dx * dx + dy * dy <= r * r) return n;
    }
    return null;
  }, []);

  const onPointerDown = useCallback((evt: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = evt.currentTarget.getBoundingClientRect();
    const x = evt.clientX - rect.left;
    const y = evt.clientY - rect.top;
    const hit = hitTest(x, y);
    if (hit) {
      hit.pinned = true;
      hit.vx = 0;
      hit.vy = 0;
      draggingRef.current = hit;
      setSelected(hit);
      alphaRef.current = 0.4;
      evt.currentTarget.setPointerCapture(evt.pointerId);
    } else {
      setSelected(null);
    }
  }, [hitTest]);

  const onPointerMove = useCallback((evt: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = evt.currentTarget.getBoundingClientRect();
    const x = evt.clientX - rect.left;
    const y = evt.clientY - rect.top;
    const drag = draggingRef.current;
    if (drag) {
      drag.x = x;
      drag.y = y;
    } else {
      const hit = hitTest(x, y);
      hoverRef.current = hit;
      evt.currentTarget.style.cursor = hit ? "pointer" : "default";
    }
  }, [hitTest]);

  const onPointerUp = useCallback((evt: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = draggingRef.current;
    if (drag) {
      drag.pinned = false;
      draggingRef.current = null;
      alphaRef.current = Math.max(alphaRef.current, 0.3);
      evt.currentTarget.releasePointerCapture(evt.pointerId);
    }
  }, []);

  const typesLegend = useMemo(() => {
    if (!data) return [] as { type: string; color: string }[];
    return data.stats.types.map((t) => ({ type: t, color: colorForType(t) }));
  }, [data]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2">
        <Network className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-sm font-semibold">Memory graph</h1>
        {data ? (
          <span className="text-xs text-muted-foreground">
            {data.stats.node_count} nodes · {data.stats.edge_count} edges
            {data.stats.phantom_count > 0
              ? ` · ${data.stats.phantom_count} phantom`
              : ""}
            {data.stats.truncated_nodes || data.stats.truncated_edges
              ? " · truncated"
              : ""}
          </span>
        ) : null}
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter nodes…"
          className={cn(
            "ml-auto h-7 w-48 rounded-md border border-input bg-background px-2 text-[12.5px]",
            "outline-none focus:ring-1 focus:ring-ring",
          )}
        />
        <Button
          variant="ghost"
          size="icon"
          aria-label="Refresh"
          onClick={() => void refresh()}
          disabled={loading}
          className="h-7 w-7"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
        </Button>
      </header>

      <div ref={wrapRef} className="relative min-h-0 flex-1">
        {error ? (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-destructive">
            {error}
          </div>
        ) : null}
        {loading && !data ? (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
            Loading…
          </div>
        ) : null}
        {data && data.nodes.length === 0 ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-sm text-muted-foreground">
            <span>No entity pages yet.</span>
            <span className="text-xs">
              Run <code className="rounded bg-muted px-1">durin memory dream</code>{" "}
              once entries accumulate.
            </span>
          </div>
        ) : null}
        <canvas
          ref={canvasRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={() => {
            hoverRef.current = null;
          }}
          className="block h-full w-full"
        />

        {/* Legend */}
        {typesLegend.length > 0 ? (
          <div className="pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-x-3 gap-y-1 rounded-md bg-background/85 px-2 py-1 text-[11px] text-muted-foreground backdrop-blur">
            {typesLegend.map((t) => (
              <span key={t.type} className="flex items-center gap-1">
                <span
                  className="inline-block h-2.5 w-2.5 rounded-full"
                  style={{ background: t.color }}
                />
                {t.type}
              </span>
            ))}
            <span className="flex items-center gap-1">
              <span className="inline-block h-2.5 w-2.5 rounded-full border border-dashed border-foreground/50" />
              phantom
            </span>
          </div>
        ) : null}

        {/* Detail panel for selected node */}
        {selected ? (
          <aside className="absolute right-3 top-3 w-72 max-w-[calc(100vw-1.5rem)] rounded-lg border border-border/50 bg-card/95 p-3 text-sm shadow-lg backdrop-blur">
            <div className="flex items-start gap-2">
              <span
                className="mt-1 inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ background: colorForType(selected.type) }}
              />
              <div className="min-w-0 flex-1">
                <div className="truncate font-semibold">{selected.name}</div>
                <div className="truncate text-xs text-muted-foreground">
                  {selected.id}
                  {selected.phantom ? " · phantom" : ""}
                </div>
              </div>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Close"
                onClick={() => setSelected(null)}
                className="h-6 w-6"
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
            <Separator className="my-2" />
            <dl className="space-y-1.5 text-xs">
              <div className="flex justify-between gap-2">
                <dt className="text-muted-foreground">Type</dt>
                <dd className="font-mono">{selected.type}</dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted-foreground">Entries referencing</dt>
                <dd className="font-mono">{selected.weight}</dd>
              </div>
              {selected.aliases.length > 0 ? (
                <div>
                  <dt className="text-muted-foreground">Aliases</dt>
                  <dd className="mt-0.5 flex flex-wrap gap-1">
                    {selected.aliases.map((a) => (
                      <span
                        key={a}
                        className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10.5px]"
                      >
                        {a}
                      </span>
                    ))}
                  </dd>
                </div>
              ) : null}
            </dl>
            {selected.phantom ? (
              <p className="mt-2 text-[11px] text-muted-foreground">
                Tagged in episodic entries but no consolidated page yet.
                Run <code className="rounded bg-muted px-1">durin memory dream</code>{" "}
                to create one.
              </p>
            ) : null}
          </aside>
        ) : null}
      </div>
    </div>
  );
}
