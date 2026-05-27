import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Focus,
  Maximize2,
  Minimize2,
  Network,
  RefreshCw,
  Search as SearchIcon,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useMemoryGraph } from "@/hooks/useMemoryGraph";
import { useClient } from "@/providers/ClientProvider";
import {
  ApiError,
  fetchMemoryEdge,
  fetchMemoryEntity,
  fetchMemorySession,
  searchMemoryApi,
  type MemoryEdgeDetail,
  type MemoryEntityDetail,
  type MemoryGraphNode,
  type MemorySearchPayload,
  type MemorySessionDetail,
} from "@/lib/api";
import { cn } from "@/lib/utils";

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

const TYPE_PALETTE: Record<string, string> = {
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
};
const FALLBACK_HUES = [200, 25, 145, 285, 60, 320, 95];

function colorForType(type: string): string {
  if (TYPE_PALETTE[type]) return TYPE_PALETTE[type];
  let h = 0;
  for (let i = 0; i < type.length; i++) h = (h * 31 + type.charCodeAt(i)) >>> 0;
  const hue = FALLBACK_HUES[h % FALLBACK_HUES.length];
  return `hsl(${hue} 65% 55%)`;
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

function radiusForWeight(weight: number): number {
  return 5 + Math.sqrt(Math.max(0, weight)) * 2.2;
}

/** Cap a node label to a sensible visual length without dropping the
 *  identifying suffix. Sessions can have long UUID-ish names; we
 *  ellipsise the middle so both ends stay readable. */
function shortLabel(label: string, max = 22): string {
  if (label.length <= max) return label;
  const headLen = Math.max(8, Math.floor(max * 0.55));
  const tailLen = Math.max(4, max - headLen - 1);
  return `${label.slice(0, headLen)}…${label.slice(-tailLen)}`;
}

function tickForces(
  nodes: SimNode[],
  edges: SimEdge[],
  width: number,
  height: number,
  alpha: number,
) {
  const cx = width / 2;
  const cy = height / 2;
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
      const sizeBoost = 1 + Math.log(2 + (a.weight ?? 0) + (b.weight ?? 0)) * 0.4;
      const k = (2200 * sizeBoost) / d2;
      fx += (dx / d) * k;
      fy += (dy / d) * k;
    }
    a.vx = (a.vx + fx * alpha) * 0.82;
    a.vy = (a.vy + fy * alpha) * 0.82;
  }
  for (const e of edges) {
    const dx = e.target.x - e.source.x;
    const dy = e.target.y - e.source.y;
    const d = Math.sqrt(dx * dx + dy * dy + 0.01);
    const rest = 90;
    const k = 0.03 * Math.min(4, e.weight);
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

type TabName = "info" | "body" | "history" | "sources" | "archive";
type SessionTabName = "info" | "messages" | "events" | "memory_ops" | "entries";

export function MemoryGraphView(_props: MemoryGraphViewProps) {
  const { data, loading, error, refresh } = useMemoryGraph(_props.active);
  const { token } = useClient();
  const tokenRef = useRef(token);
  tokenRef.current = token;

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const simNodesRef = useRef<SimNode[]>([]);
  const simEdgesRef = useRef<SimEdge[]>([]);
  const alphaRef = useRef(1);
  const rafRef = useRef<number | null>(null);
  const draggingRef = useRef<SimNode | null>(null);
  const hoverRef = useRef<SimNode | null>(null);

  // Side panel state — branches by selected.type:
  //   - "session" → fetch MemorySessionDetail, render session tabs
  //   - everything else → fetch MemoryEntityDetail, render entity tabs
  const [selected, setSelected] = useState<MemoryGraphNode | null>(null);
  const [detail, setDetail] = useState<MemoryEntityDetail | null>(null);
  const [sessionDetail, setSessionDetail] =
    useState<MemorySessionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabName>("info");
  const [sessionTab, setSessionTab] = useState<SessionTabName>("info");
  const [focusRef, setFocusRef] = useState<string | null>(null);
  const isSessionSelected = selected?.type === "session";
  // Wide-mode toggle for the right-hand detail panel. Sessions can
  // accumulate long tool outputs that get cramped in the default
  // ~26rem column; expand to ~80% of the viewport when the user
  // needs to read full message bodies. Persisted so the choice
  // survives reloads.
  const [panelExpanded, setPanelExpanded] = useState<boolean>(() => {
    try {
      return localStorage.getItem("durin.memoryGraph.panelExpanded") === "1";
    } catch {
      return false;
    }
  });
  const togglePanelExpanded = useCallback(() => {
    setPanelExpanded((cur) => {
      const next = !cur;
      try {
        localStorage.setItem(
          "durin.memoryGraph.panelExpanded",
          next ? "1" : "0",
        );
      } catch {
        /* localStorage unavailable: ephemeral toggle is fine */
      }
      return next;
    });
  }, []);
  // Set of node types the user has toggled OFF in the legend. Default
  // empty = show all. Clicking a legend chip flips inclusion. Phantom
  // is treated as its own pseudo-type for the toggle.
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());

  function toggleType(type: string): void {
    setHiddenTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  // Search panel state
  const [search, setSearch] = useState("");
  const [searchResults, setSearchResults] =
    useState<MemorySearchPayload | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);

  // Edge popup state
  const [edgePopup, setEdgePopup] = useState<{
    x: number; y: number; detail: MemoryEdgeDetail | null; loading: boolean;
  } | null>(null);

  // Build simulation arrays from data
  const { simNodes, simEdges } = useMemo(() => {
    if (!data) return { simNodes: [] as SimNode[], simEdges: [] as SimEdge[] };
    const w = wrapRef.current?.clientWidth ?? 800;
    const h = wrapRef.current?.clientHeight ?? 600;
    const cx = w / 2;
    const cy = h / 2;
    const sims: SimNode[] = data.nodes.map((n, i) => {
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

  // Compute neighbour set for the active focus ref (1-hop)
  const focusNeighbours = useMemo(() => {
    if (!focusRef || !data) return null;
    const set = new Set<string>([focusRef]);
    for (const e of data.edges) {
      if (e.source === focusRef) set.add(e.target);
      else if (e.target === focusRef) set.add(e.source);
    }
    return set;
  }, [focusRef, data]);

  // Compute matching ref set for search dimming
  const searchMatchSet = useMemo(() => {
    if (!searchResults) return null;
    const refs = new Set<string>();
    for (const r of searchResults.results) {
      // Result URI: memory/<class_name>/<id>; for entity_page rows the
      // id IS the entity ref. For episodic rows, the entities[] field
      // carries the refs.
      if (r.class_name === "entity_page") {
        const id = r.uri.split("/").pop() ?? "";
        refs.add(id);
      }
      for (const ref of r.entities ?? []) {
        refs.add(ref);
      }
    }
    return refs.size > 0 ? refs : null;
  }, [searchResults]);

  // RAF render loop
  useEffect(() => {
    if (!_props.active) return;
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

    function isHighlighted(id: string): boolean {
      // A node is highlighted iff it passes BOTH active dimming layers.
      // (focus and search compose multiplicatively — focus AND search
      // hit both must be true.)
      if (focusNeighbours && !focusNeighbours.has(id)) return false;
      if (searchMatchSet && !searchMatchSet.has(id)) return false;
      return true;
    }

    function isVisible(node: SimNode): boolean {
      // Type-toggle: legend chips hide whole categories at a time.
      // Phantom is treated as its own pseudo-type so the user can
      // hide unconsolidated noise without losing the entity types.
      if (hiddenTypes.has(node.type)) return false;
      if (node.phantom && hiddenTypes.has("phantom")) return false;
      return true;
    }

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

      const alpha = alphaRef.current;
      if (alpha > 0.02) {
        tickForces(nodes, edges, w, h, alpha);
        alphaRef.current = alpha * 0.985;
      }

      ctx.clearRect(0, 0, w, h);

      ctx.lineCap = "round";
      for (const e of edges) {
        // Hidden endpoints → don't draw the edge at all.
        if (!isVisible(e.source) || !isVisible(e.target)) continue;
        const lit = isHighlighted(e.source.id) && isHighlighted(e.target.id);
        ctx.strokeStyle = lit
          ? `rgba(120,120,140,${Math.min(0.55, 0.18 + e.weight * 0.06)})`
          : "rgba(120,120,140,0.08)";
        ctx.lineWidth = Math.min(3, 0.8 + Math.log(1 + e.weight));
        ctx.beginPath();
        ctx.moveTo(e.source.x, e.source.y);
        ctx.lineTo(e.target.x, e.target.y);
        ctx.stroke();
      }

      for (const n of nodes) {
        if (!isVisible(n)) continue;
        const r = radiusForWeight(n.weight);
        const lit = isHighlighted(n.id);
        const fill = colorForType(n.type);
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = lit ? fill : `${fill}33`;
        ctx.fill();
        if (n.phantom) {
          ctx.setLineDash([3, 3]);
          ctx.strokeStyle = lit ? "rgba(0,0,0,0.4)" : "rgba(0,0,0,0.15)";
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.setLineDash([]);
        }
        if (
          selected?.id === n.id ||
          hoverRef.current?.id === n.id ||
          focusRef === n.id
        ) {
          ctx.beginPath();
          ctx.arc(n.x, n.y, r + 3, 0, Math.PI * 2);
          ctx.strokeStyle =
            focusRef === n.id ? "rgba(20,40,200,0.75)" : "rgba(0,0,0,0.55)";
          ctx.lineWidth = 1.6;
          ctx.stroke();
        }
      }

      ctx.font = "11px ui-sans-serif, system-ui, -apple-system";
      ctx.textBaseline = "top";
      ctx.textAlign = "center";
      for (const n of nodes) {
        if (!isVisible(n)) continue;
        const r = radiusForWeight(n.weight);
        const lit = isHighlighted(n.id);
        const shouldLabel =
          r > 9 || lit || selected?.id === n.id || focusRef === n.id;
        if (!shouldLabel) continue;
        ctx.fillStyle = lit ? "rgba(0,0,0,0.75)" : "rgba(0,0,0,0.30)";
        ctx.fillText(shortLabel(n.name), n.x, n.y + r + 2);
      }

      rafRef.current = requestAnimationFrame(frame);
    }
    rafRef.current = requestAnimationFrame(frame);

    return () => {
      stopped = true;
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      ro.disconnect();
    };
  }, [_props.active, selected, focusRef, focusNeighbours, searchMatchSet, hiddenTypes]);

  // Hit-test (for nodes AND edges). Skips nodes hidden by legend
  // toggles so the user can't accidentally select a node that's not
  // even rendered.
  const hitTestNode = useCallback((x: number, y: number): SimNode | null => {
    const nodes = simNodesRef.current;
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      if (hiddenTypes.has(n.type)) continue;
      if (n.phantom && hiddenTypes.has("phantom")) continue;
      const r = radiusForWeight(n.weight) + 4;
      const dx = x - n.x;
      const dy = y - n.y;
      if (dx * dx + dy * dy <= r * r) return n;
    }
    return null;
  }, [hiddenTypes]);

  const hitTestEdge = useCallback((x: number, y: number): SimEdge | null => {
    // Distance from point (x,y) to each line segment; pick the closest
    // under a tolerance of 6px. Skip edges with hidden endpoints to
    // match the visual render.
    const edges = simEdgesRef.current;
    let best: { e: SimEdge; d: number } | null = null;
    for (const e of edges) {
      if (hiddenTypes.has(e.source.type) || hiddenTypes.has(e.target.type)) continue;
      if ((e.source.phantom || e.target.phantom) && hiddenTypes.has("phantom")) continue;
      const x1 = e.source.x, y1 = e.source.y;
      const x2 = e.target.x, y2 = e.target.y;
      const dx = x2 - x1, dy = y2 - y1;
      const lenSq = dx * dx + dy * dy;
      if (lenSq < 1) continue;
      let t = ((x - x1) * dx + (y - y1) * dy) / lenSq;
      t = clamp(t, 0, 1);
      const px = x1 + t * dx, py = y1 + t * dy;
      const d = Math.sqrt((x - px) ** 2 + (y - py) ** 2);
      if (d < 6 && (best == null || d < best.d)) best = { e, d };
    }
    return best?.e ?? null;
  }, [hiddenTypes]);

  const onPointerDown = useCallback(
    (evt: React.PointerEvent<HTMLCanvasElement>) => {
      const rect = evt.currentTarget.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const y = evt.clientY - rect.top;
      const hit = hitTestNode(x, y);
      if (hit) {
        hit.pinned = true;
        hit.vx = 0;
        hit.vy = 0;
        draggingRef.current = hit;
        setSelected(hit);
        setActiveTab("info");
        setEdgePopup(null);
        alphaRef.current = 0.4;
        evt.currentTarget.setPointerCapture(evt.pointerId);
        return;
      }
      const edgeHit = hitTestEdge(x, y);
      if (edgeHit) {
        // Open edge popup near the midpoint
        const mx = (edgeHit.source.x + edgeHit.target.x) / 2;
        const my = (edgeHit.source.y + edgeHit.target.y) / 2;
        setEdgePopup({ x: mx, y: my, detail: null, loading: true });
        setSelected(null);
        void (async () => {
          if (!tokenRef.current) return;
          try {
            const d = await fetchMemoryEdge(
              tokenRef.current,
              edgeHit.source.id,
              edgeHit.target.id,
            );
            setEdgePopup({ x: mx, y: my, detail: d, loading: false });
          } catch (e) {
            const msg = e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
            setEdgePopup({
              x: mx, y: my,
              detail: { source: edgeHit.source.id, target: edgeHit.target.id, total: 0, entries: [] },
              loading: false,
            });
            console.error("edge detail fetch failed", msg);
          }
        })();
        return;
      }
      setSelected(null);
      setEdgePopup(null);
    },
    [hitTestNode, hitTestEdge],
  );

  const onPointerMove = useCallback(
    (evt: React.PointerEvent<HTMLCanvasElement>) => {
      const rect = evt.currentTarget.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const y = evt.clientY - rect.top;
      const drag = draggingRef.current;
      if (drag) {
        drag.x = x;
        drag.y = y;
      } else {
        const hit = hitTestNode(x, y) || hitTestEdge(x, y);
        hoverRef.current = (hit && "weight" in hit && !("source" in hit)) ? (hit as SimNode) : null;
        evt.currentTarget.style.cursor = hit ? "pointer" : "default";
      }
    },
    [hitTestNode, hitTestEdge],
  );

  const onPointerUp = useCallback(
    (evt: React.PointerEvent<HTMLCanvasElement>) => {
      const drag = draggingRef.current;
      if (drag) {
        drag.pinned = false;
        draggingRef.current = null;
        alphaRef.current = Math.max(alphaRef.current, 0.3);
        evt.currentTarget.releasePointerCapture(evt.pointerId);
      }
    },
    [],
  );

  // Fetch detail whenever the selection changes — branch by type.
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setSessionDetail(null);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    setDetail(null);
    setSessionDetail(null);
    (async () => {
      if (!tokenRef.current) return;
      try {
        if (selected.type === "session") {
          // session:<stem> → strip prefix, fetch session detail.
          const stem = selected.id.replace(/^session:/, "");
          const d = await fetchMemorySession(tokenRef.current, stem);
          if (!cancelled) setSessionDetail(d);
        } else {
          const d = await fetchMemoryEntity(tokenRef.current, selected.id);
          if (!cancelled) setDetail(d);
        }
      } catch (e) {
        if (!cancelled) {
          const msg = e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
          setDetailError(msg);
        }
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // Run search whenever the query stabilises
  useEffect(() => {
    const q = search.trim();
    if (!q) {
      setSearchResults(null);
      setSearchError(null);
      return;
    }
    let cancelled = false;
    const handle = setTimeout(() => {
      setSearchLoading(true);
      (async () => {
        if (!tokenRef.current) return;
        try {
          const r = await searchMemoryApi(tokenRef.current, q);
          if (!cancelled) {
            setSearchResults(r);
            setSearchError(null);
          }
        } catch (e) {
          if (!cancelled) {
            const msg = e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
            setSearchError(msg);
            setSearchResults(null);
          }
        } finally {
          if (!cancelled) setSearchLoading(false);
        }
      })();
    }, 220);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [search]);

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
        <div className="ml-auto flex items-center gap-2">
          <div className="relative">
            <SearchIcon
              className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <input
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setSearchOpen(true);
              }}
              onFocus={() => setSearchOpen(true)}
              placeholder="Search memory (vector + grep)…"
              className={cn(
                "h-7 w-72 rounded-md border border-input bg-background pl-7 pr-2 text-[12.5px]",
                "outline-none focus:ring-1 focus:ring-ring",
              )}
            />
            {search ? (
              <button
                type="button"
                aria-label="Clear"
                onClick={() => {
                  setSearch("");
                  setSearchResults(null);
                }}
                className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:bg-muted"
              >
                <X className="h-3 w-3" />
              </button>
            ) : null}
          </div>
          {focusRef ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setFocusRef(null)}
              className="h-7 gap-1 text-[11px]"
            >
              <Focus className="h-3 w-3" /> Unfocus
            </Button>
          ) : null}
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
        </div>
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

        {/* Legend bottom-left — chips are click-to-toggle filters.
            Clicking a chip hides every node of that type; clicking
            again restores. Phantom is its own pseudo-type. */}
        {typesLegend.length > 0 ? (
          <div className="absolute bottom-3 left-3 flex flex-wrap items-center gap-1 rounded-md bg-background/85 p-1 text-[11px] backdrop-blur">
            {typesLegend.map((t) => {
              const hidden = hiddenTypes.has(t.type);
              return (
                <button
                  key={t.type}
                  type="button"
                  onClick={() => toggleType(t.type)}
                  aria-pressed={!hidden}
                  title={hidden ? `Show ${t.type}` : `Hide ${t.type}`}
                  className={cn(
                    "flex items-center gap-1 rounded px-1.5 py-0.5 transition-opacity",
                    "hover:bg-muted",
                    hidden ? "opacity-40" : "opacity-100",
                  )}
                >
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-full"
                    style={{ background: t.color }}
                  />
                  <span className={cn(hidden && "line-through")}>{t.type}</span>
                </button>
              );
            })}
            {data && data.stats.phantom_count > 0 ? (
              <button
                type="button"
                onClick={() => toggleType("phantom")}
                aria-pressed={!hiddenTypes.has("phantom")}
                title={hiddenTypes.has("phantom") ? "Show phantom" : "Hide phantom"}
                className={cn(
                  "flex items-center gap-1 rounded px-1.5 py-0.5 transition-opacity hover:bg-muted",
                  hiddenTypes.has("phantom") ? "opacity-40" : "opacity-100",
                )}
              >
                <span className="inline-block h-2.5 w-2.5 rounded-full border border-dashed border-foreground/50" />
                <span className={cn(hiddenTypes.has("phantom") && "line-through")}>
                  phantom
                </span>
              </button>
            ) : null}
            {hiddenTypes.size > 0 ? (
              <button
                type="button"
                onClick={() => setHiddenTypes(new Set())}
                className="ml-1 rounded border border-border/40 px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-muted"
              >
                Show all
              </button>
            ) : null}
          </div>
        ) : null}

        {/* Search results panel (left side, slides over) */}
        {searchOpen && search.trim() ? (
          <aside className="absolute bottom-12 left-3 top-3 z-10 w-80 max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-lg border border-border/50 bg-card/95 shadow-lg backdrop-blur">
            <header className="flex items-center justify-between border-b border-border/40 px-3 py-2 text-xs">
              <span className="font-semibold">
                Search · {searchResults ? `${searchResults.total} results` : "…"}
              </span>
              <div className="flex items-center gap-1 text-muted-foreground">
                {searchResults ? (
                  <span>{searchResults.strategy}·{searchResults.ranking}</span>
                ) : null}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-5 w-5"
                  onClick={() => setSearchOpen(false)}
                  aria-label="Close search panel"
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            </header>
            <div className="max-h-full overflow-y-auto">
              {searchLoading ? (
                <div className="px-3 py-3 text-xs text-muted-foreground">Searching…</div>
              ) : null}
              {searchError ? (
                <div className="px-3 py-3 text-xs text-destructive">{searchError}</div>
              ) : null}
              {searchResults && searchResults.results.length === 0 && !searchLoading ? (
                <div className="px-3 py-3 text-xs text-muted-foreground">
                  No matches.
                </div>
              ) : null}
              {searchResults?.results.slice(0, 40).map((r, idx) => {
                const isCanon = r.kind === "canonical";
                const id = isCanon ? r.uri.split("/").pop() ?? "" : r.uri;
                return (
                  <button
                    type="button"
                    key={`${r.uri}-${idx}`}
                    onClick={() => {
                      // For canonical: select that node in the graph if present.
                      if (isCanon) {
                        const node = simNodesRef.current.find((n) => n.id === id);
                        if (node) {
                          setSelected(node);
                          setActiveTab("info");
                        }
                      }
                    }}
                    className="block w-full border-t border-border/30 px-3 py-2 text-left text-xs hover:bg-muted/60"
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className={cn(
                          "rounded px-1 text-[10px] uppercase tracking-wide",
                          isCanon
                            ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                            : "bg-amber-500/15 text-amber-700 dark:text-amber-400",
                        )}
                      >
                        {r.kind}
                      </span>
                      <span className="truncate font-medium">{r.headline || r.uri}</span>
                    </div>
                    {r.snippet ? (
                      <p className="mt-1 line-clamp-2 text-[11px] text-muted-foreground">
                        {r.snippet}
                      </p>
                    ) : null}
                    {r.valid_from ? (
                      <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                        {r.valid_from.slice(0, 19)}
                      </div>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </aside>
        ) : null}

        {/* Edge popup */}
        {edgePopup ? (
          <div
            className="absolute z-20 w-72 max-w-[calc(100vw-1.5rem)] rounded-lg border border-border/50 bg-card/95 p-2.5 text-xs shadow-lg backdrop-blur"
            style={{
              left: clamp(edgePopup.x + 10, 8, (wrapRef.current?.clientWidth ?? 800) - 300),
              top: clamp(edgePopup.y + 10, 8, (wrapRef.current?.clientHeight ?? 600) - 240),
            }}
          >
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="truncate font-semibold">
                {edgePopup.detail ? (
                  <>
                    {edgePopup.detail.source} ↔ {edgePopup.detail.target}
                  </>
                ) : (
                  "Edge"
                )}
              </span>
              <Button
                variant="ghost"
                size="icon"
                className="h-5 w-5"
                onClick={() => setEdgePopup(null)}
                aria-label="Close edge popup"
              >
                <X className="h-3 w-3" />
              </Button>
            </div>
            {edgePopup.loading ? (
              <div className="text-muted-foreground">Loading…</div>
            ) : edgePopup.detail ? (
              <>
                <div className="mb-1 text-muted-foreground">
                  {edgePopup.detail.total} co-mention
                  {edgePopup.detail.total === 1 ? "" : "s"}
                </div>
                <ul className="max-h-48 space-y-1 overflow-y-auto">
                  {edgePopup.detail.entries.slice(0, 12).map((e) => (
                    <li
                      key={e.id}
                      className="rounded border border-border/40 bg-background/60 p-1.5"
                    >
                      <div className="font-mono text-[10px] text-muted-foreground">
                        {e.valid_from.slice(0, 10)} · {e.id.slice(0, 8)}
                      </div>
                      <div className="truncate">{e.headline || e.snippet}</div>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>
        ) : null}

        {/* Right-side detail panel for the selected node. Width
            toggles via the maximize button: 26rem (default, leaves
            most of the graph visible) → up to ~80vw (long tool
            outputs become readable). */}
        {selected ? (
          <aside
            className={cn(
              "absolute right-3 top-3 z-10 flex max-w-[calc(100vw-1.5rem)] flex-col rounded-lg border border-border/50 bg-card/95 text-sm shadow-lg backdrop-blur",
              "transition-[width] duration-200 ease-out",
              panelExpanded ? "w-[min(80vw,72rem)]" : "w-[26rem]",
            )}
            style={{ maxHeight: "calc(100% - 1.5rem)" }}
          >
            <header className="flex items-start gap-2 border-b border-border/40 px-3 py-2">
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
                aria-label={focusRef === selected.id ? "Unfocus" : "Focus 1-hop"}
                onClick={() =>
                  setFocusRef((c) => (c === selected.id ? null : selected.id))
                }
                className={cn(
                  "h-6 w-6",
                  focusRef === selected.id && "bg-primary/10 text-primary",
                )}
              >
                <Focus className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                aria-label={panelExpanded ? "Collapse panel" : "Expand panel"}
                onClick={togglePanelExpanded}
                className="h-6 w-6"
                title={panelExpanded ? "Collapse panel" : "Expand panel"}
              >
                {panelExpanded ? (
                  <Minimize2 className="h-3.5 w-3.5" />
                ) : (
                  <Maximize2 className="h-3.5 w-3.5" />
                )}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Close"
                onClick={() => {
                  setSelected(null);
                  if (focusRef) setFocusRef(null);
                }}
                className="h-6 w-6"
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </header>

            <div className="flex shrink-0 flex-wrap gap-1 border-b border-border/30 px-2 py-1.5 text-[11px]">
              {isSessionSelected
                ? (
                    [
                      { id: "info", label: "Info" },
                      { id: "messages", label: `Messages${sessionDetail?.recent_messages.length ? ` (${sessionDetail.recent_messages.length})` : ""}` },
                      { id: "events", label: `Events${sessionDetail?.events.length ? ` (${sessionDetail.events.length})` : ""}` },
                      { id: "memory_ops", label: `Memory ops${sessionDetail?.memory_ops.length ? ` (${sessionDetail.memory_ops.length})` : ""}` },
                      { id: "entries", label: `Entries${sessionDetail?.entries_linked.length ? ` (${sessionDetail.entries_linked.length})` : ""}` },
                    ] as const
                  ).map((t) => (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => setSessionTab(t.id as SessionTabName)}
                      className={cn(
                        "rounded px-2 py-1 font-medium transition-colors",
                        sessionTab === t.id
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground hover:bg-muted",
                      )}
                    >
                      {t.label}
                    </button>
                  ))
                : (
                    [
                      { id: "info", label: "Info" },
                      { id: "body", label: "Body" },
                      { id: "history", label: `History${detail?.history.length ? ` (${detail.history.length})` : ""}` },
                      { id: "sources", label: `Sources${detail?.entries.length ? ` (${detail.entries.length})` : ""}` },
                      { id: "archive", label: `Archive${detail?.archive.length ? ` (${detail.archive.length})` : ""}` },
                    ] as const
                  ).map((t) => (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => setActiveTab(t.id as TabName)}
                      className={cn(
                        "rounded px-2 py-1 font-medium transition-colors",
                        activeTab === t.id
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground hover:bg-muted",
                      )}
                    >
                      {t.label}
                    </button>
                  ))}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2 text-xs">
              {detailLoading ? (
                <div className="text-muted-foreground">Loading detail…</div>
              ) : null}
              {detailError ? (
                <div className="text-destructive">{detailError}</div>
              ) : null}
              {!detail && !sessionDetail && !detailLoading && selected.phantom ? (
                <p className="text-[11px] text-muted-foreground">
                  Tagged in episodic entries but no consolidated page yet. Run{" "}
                  <code className="rounded bg-muted px-1">durin memory dream</code>{" "}
                  to create one.
                </p>
              ) : null}
              {sessionDetail ? (
                <SessionTabs
                  detail={sessionDetail}
                  tab={sessionTab}
                />
              ) : null}
              {detail ? (
                <>
                  {activeTab === "info" ? (
                    <dl className="space-y-2">
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">Type</dt>
                        <dd className="font-mono">{detail.page.type}</dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">
                          Entries referencing
                        </dt>
                        <dd className="font-mono">{selected.weight}</dd>
                      </div>
                      {detail.page.dream_processed_through ? (
                        <div className="flex justify-between gap-2">
                          <dt className="text-muted-foreground">Last dreamed</dt>
                          <dd className="font-mono text-[11px]">
                            {detail.page.dream_processed_through}
                          </dd>
                        </div>
                      ) : null}
                      {detail.page.aliases.length > 0 ? (
                        <div>
                          <dt className="text-muted-foreground">Aliases</dt>
                          <dd className="mt-0.5 flex flex-wrap gap-1">
                            {detail.page.aliases.map((a) => (
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
                      {detail.page.identifiers ? (
                        <div>
                          <dt className="text-muted-foreground">Identifiers</dt>
                          <dd className="mt-0.5 space-y-0.5">
                            {Object.entries(detail.page.identifiers).map(
                              ([k, v]) => (
                                <div key={k} className="text-[11px]">
                                  <span className="font-mono text-muted-foreground">
                                    {k}:
                                  </span>{" "}
                                  {Array.isArray(v) ? v.join(", ") : String(v)}
                                </div>
                              ),
                            )}
                          </dd>
                        </div>
                      ) : null}
                    </dl>
                  ) : null}

                  {activeTab === "body" ? (
                    detail.page.body ? (
                      <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed">
                        {detail.page.body}
                      </pre>
                    ) : (
                      <p className="text-muted-foreground">No body content.</p>
                    )
                  ) : null}

                  {activeTab === "history" ? (
                    detail.history.length === 0 ? (
                      <p className="text-muted-foreground">No git history yet.</p>
                    ) : (
                      <ul className="space-y-2">
                        {detail.history.map((c) => (
                          <CommitItem key={c.sha} commit={c} />
                        ))}
                      </ul>
                    )
                  ) : null}

                  {activeTab === "sources" ? (
                    detail.entries.length === 0 ? (
                      <p className="text-muted-foreground">
                        No post-cursor entries — everything has been consolidated.
                      </p>
                    ) : (
                      <ul className="space-y-2">
                        {detail.entries.map((e) => (
                          <li
                            key={e.id}
                            className="rounded border border-border/40 bg-background/60 p-2"
                          >
                            <div className="flex items-center justify-between text-[10.5px] text-muted-foreground">
                              <span className="font-mono">{e.id.slice(0, 8)}</span>
                              <span>{e.valid_from.slice(0, 10)}</span>
                            </div>
                            {e.headline ? (
                              <div className="mt-0.5 font-medium">{e.headline}</div>
                            ) : null}
                            {e.summary ? (
                              <div className="mt-0.5 text-[11px]">{e.summary}</div>
                            ) : null}
                            {e.body ? (
                              <details className="mt-1">
                                <summary className="cursor-pointer text-[10.5px] text-muted-foreground">
                                  body
                                </summary>
                                <pre className="mt-1 max-h-56 overflow-y-auto whitespace-pre-wrap text-[10.5px] leading-relaxed">
                                  {e.body}
                                </pre>
                              </details>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )
                  ) : null}

                  {activeTab === "archive" ? (
                    detail.archive.length === 0 ? (
                      <p className="text-muted-foreground">
                        No absorptions for this entity.
                      </p>
                    ) : (
                      <ul className="space-y-2">
                        {detail.archive.map((a) => (
                          <li
                            key={a.slug}
                            className="rounded border border-border/40 bg-background/60 p-2"
                          >
                            <div className="font-medium">{a.name}</div>
                            <div className="font-mono text-[10.5px] text-muted-foreground">
                              {a.slug}
                            </div>
                            {a.archived_at ? (
                              <div className="mt-0.5 text-[10.5px] text-muted-foreground">
                                Archived: {a.archived_at.slice(0, 19)}
                              </div>
                            ) : null}
                            {a.archived_reason ? (
                              <div className="text-[10.5px] text-muted-foreground">
                                Reason: {a.archived_reason}
                              </div>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )
                  ) : null}
                </>
              ) : null}
            </div>
            <Separator className="bg-border/30" />
            <footer className="px-3 py-1.5 text-[10.5px] text-muted-foreground">
              Click another node, drag to pin, click the focus icon to isolate
              1-hop neighbours.
            </footer>
          </aside>
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-component for collapsible commit details (history tab)
// ---------------------------------------------------------------------------


function SessionTabs({
  detail,
  tab,
}: {
  detail: MemorySessionDetail;
  tab: SessionTabName;
}) {
  if (tab === "info") {
    const info = detail.info;
    const metaEnts = detail.entities_tagged.from_meta;
    const refEnts = detail.entities_tagged.from_source_refs;
    return (
      <dl className="space-y-2">
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">Session key</dt>
          <dd className="font-mono">{detail.session_key ?? detail.session_ref}</dd>
        </div>
        {info.channel ? (
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Channel</dt>
            <dd className="font-mono">{info.channel}</dd>
          </div>
        ) : null}
        {info.model ? (
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Model</dt>
            <dd className="font-mono">{info.model}</dd>
          </div>
        ) : null}
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">Messages</dt>
          <dd className="font-mono">{info.message_count}</dd>
        </div>
        {info.created_at ? (
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Created</dt>
            <dd className="font-mono text-[11px]">{info.created_at.slice(0, 19)}</dd>
          </div>
        ) : null}
        {info.updated_at ? (
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Updated</dt>
            <dd className="font-mono text-[11px]">{info.updated_at.slice(0, 19)}</dd>
          </div>
        ) : null}
        {metaEnts.length > 0 ? (
          <div>
            <dt className="text-muted-foreground">Entities (from meta tags)</dt>
            <dd className="mt-0.5 flex flex-wrap gap-1">
              {metaEnts.map((e) => (
                <span
                  key={e}
                  className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10.5px]"
                >
                  {e}
                </span>
              ))}
            </dd>
          </div>
        ) : null}
        {refEnts.length > 0 ? (
          <div>
            <dt className="text-muted-foreground">Entities (from entry source_refs)</dt>
            <dd className="mt-0.5 flex flex-wrap gap-1">
              {refEnts.map((e) => (
                <span
                  key={e}
                  className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10.5px]"
                >
                  {e}
                </span>
              ))}
            </dd>
          </div>
        ) : null}
      </dl>
    );
  }

  if (tab === "messages") {
    if (detail.recent_messages.length === 0) {
      return <p className="text-muted-foreground">No recent messages.</p>;
    }
    return (
      <ul className="space-y-2">
        {detail.recent_messages.map((m, i) => (
          <li
            key={i}
            className="rounded border border-border/40 bg-background/60 p-2"
          >
            <div className="flex items-center justify-between text-[10.5px] text-muted-foreground">
              <span className="font-mono uppercase">{m.role}</span>
              {m.ts ? (
                <span className="font-mono">
                  {typeof m.ts === "number" ? new Date(m.ts * 1000).toISOString().slice(0, 19) : String(m.ts).slice(0, 19)}
                </span>
              ) : null}
            </div>
            <p className="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed">
              {m.preview}
            </p>
          </li>
        ))}
      </ul>
    );
  }

  if (tab === "events") {
    if (detail.events.length === 0) {
      return <p className="text-muted-foreground">No lifecycle events recorded.</p>;
    }
    return (
      <ul className="space-y-1.5">
        {detail.events.map((ev, i) => (
          <li
            key={i}
            className="rounded border border-border/40 bg-background/60 p-2 text-[11px]"
          >
            <div className="flex items-center justify-between">
              <span className="font-mono uppercase text-muted-foreground">
                {String((ev as Record<string, unknown>).type ?? "event")}
              </span>
              <span className="font-mono text-[10px] text-muted-foreground">
                {String((ev as Record<string, unknown>).ts ?? (ev as Record<string, unknown>).created_at ?? "").slice(0, 19)}
              </span>
            </div>
            <pre className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap break-all text-[10.5px]">
              {JSON.stringify(ev, null, 2)}
            </pre>
          </li>
        ))}
      </ul>
    );
  }

  if (tab === "memory_ops") {
    if (detail.memory_ops.length === 0) {
      return (
        <p className="text-muted-foreground">
          No memory_* tool calls recorded in this session's events.
        </p>
      );
    }
    return (
      <ul className="space-y-2">
        {detail.memory_ops.map((op, i) => (
          <li
            key={i}
            className="rounded border border-border/40 bg-background/60 p-2 text-[11px]"
          >
            <div className="flex items-center justify-between">
              <span className="font-mono font-semibold">{op.tool}</span>
              {op.ts ? (
                <span className="font-mono text-[10px] text-muted-foreground">
                  {String(op.ts).slice(0, 19)}
                </span>
              ) : null}
            </div>
            {op.args_preview ? (
              <details className="mt-1">
                <summary className="cursor-pointer text-[10.5px] text-muted-foreground">
                  args
                </summary>
                <pre className="mt-1 whitespace-pre-wrap text-[10.5px]">
                  {op.args_preview}
                </pre>
              </details>
            ) : null}
            {op.result_preview ? (
              <details className="mt-1">
                <summary className="cursor-pointer text-[10.5px] text-muted-foreground">
                  result
                </summary>
                <pre className="mt-1 whitespace-pre-wrap text-[10.5px]">
                  {op.result_preview}
                </pre>
              </details>
            ) : null}
          </li>
        ))}
      </ul>
    );
  }

  // tab === "entries"
  if (detail.entries_linked.length === 0) {
    return (
      <p className="text-muted-foreground">
        No episodic entries linked back to this session via source_refs.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {detail.entries_linked.map((e) => (
        <li
          key={e.id}
          className="rounded border border-border/40 bg-background/60 p-2"
        >
          <div className="flex items-center justify-between text-[10.5px] text-muted-foreground">
            <span className="font-mono">{e.id.slice(0, 8)}</span>
            <span>{e.valid_from.slice(0, 10)}</span>
          </div>
          {e.headline ? (
            <div className="mt-0.5 font-medium">{e.headline}</div>
          ) : null}
          {e.snippet ? (
            <p className="mt-0.5 line-clamp-3 text-[11px]">{e.snippet}</p>
          ) : null}
          {e.entities.length > 0 ? (
            <div className="mt-1 flex flex-wrap gap-1">
              {e.entities.map((ent) => (
                <span
                  key={ent}
                  className="rounded bg-muted px-1 py-0.5 font-mono text-[10px]"
                >
                  {ent}
                </span>
              ))}
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}


function CommitItem({
  commit,
}: {
  commit: {
    sha: string;
    short_sha: string;
    subject: string;
    body: string;
    when: string;
    trailers: Record<string, string[]>;
  };
}) {
  const [open, setOpen] = useState(false);
  const trailerEntries = Object.entries(commit.trailers || {});
  const isAuto = (commit.trailers?.Reason || []).includes("auto");
  return (
    <li className="rounded border border-border/40 bg-background/60 p-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-1.5 text-left"
      >
        {open ? (
          <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10.5px] text-muted-foreground">
              {commit.short_sha}
            </span>
            {isAuto ? (
              <span className="rounded bg-amber-500/15 px-1 text-[10px] uppercase tracking-wide text-amber-700 dark:text-amber-400">
                auto
              </span>
            ) : null}
            <span className="font-mono text-[10.5px] text-muted-foreground">
              {commit.when ? commit.when.slice(0, 10) : ""}
            </span>
          </div>
          <div className="truncate text-[11.5px] font-medium">{commit.subject}</div>
        </div>
      </button>
      {open ? (
        <div className="mt-2 space-y-2">
          {commit.body ? (
            <pre className="whitespace-pre-wrap text-[10.5px] leading-relaxed text-muted-foreground">
              {commit.body}
            </pre>
          ) : null}
          {trailerEntries.length > 0 ? (
            <dl className="space-y-0.5 text-[10.5px]">
              {trailerEntries.map(([k, v]) => (
                <div key={k} className="flex gap-1">
                  <dt className="font-mono text-muted-foreground">{k}:</dt>
                  <dd className="min-w-0 flex-1 break-all">{v.join(", ")}</dd>
                </div>
              ))}
            </dl>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
