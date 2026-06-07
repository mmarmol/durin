import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Focus,
  Maximize2,
  Minimize2,
  Network,
  RefreshCw,
  Search as SearchIcon,
  Trash2,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useMemoryGraph } from "@/hooks/useMemoryGraph";
import { useClient } from "@/providers/ClientProvider";
import MarkdownTextRenderer from "@/components/MarkdownTextRenderer";
import {
  ApiError,
  fetchMemoryBacklinks,
  fetchMemoryEdge,
  fetchMemoryEntity,
  fetchMemoryEntry,
  fetchMemorySession,
  fetchMemorySubgraph,
  forgetMemoryEntry,
  searchMemoryApi,
  type MemoryBacklinksPayload,
  type MemoryEdgeDetail,
  type MemoryEntityDetail,
  type MemoryEntryDetail,
  type MemoryGraphNode,
  type MemoryGraphPayload,
  type MemorySearchPayload,
  type MemorySearchResult,
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

// Width (px) the open content panel reserves on the right — the canvas shrinks
// by this so the graph re-fits beside the panel instead of being covered.
// Returns 0 when no content panel is open (the `.mg-cpanel` aside is only
// mounted while open).
function reservedRightWidth(): number {
  if (typeof document === "undefined") return 0;
  const p = document.querySelector(".mg-cpanel");
  return p ? Math.round((p as HTMLElement).getBoundingClientRect().width) : 0;
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

type TabName = "info" | "body" | "provenance" | "history" | "sources" | "archive" | "entries";
type SessionTabName = "info" | "messages" | "events" | "memory_ops" | "entries";

export function MemoryGraphView(_props: MemoryGraphViewProps) {
  const { t } = useTranslation();
  const { data: rawData, loading, error, refresh } = useMemoryGraph(_props.active);
  // Focus mode (Obsidian local graph): when set, the canvas renders this
  // ego-graph (a node + its neighbourhood, fetched uncapped) instead of the
  // global overview, so the node is centred even if the cap dropped it.
  const [focusGraph, setFocusGraph] = useState<MemoryGraphPayload | null>(null);
  const data = focusGraph ?? rawData;
  // Reference docs (memory/references/*) aren't graph nodes; clicking a
  // reference search hit opens its content in this side panel.
  const [referenceDetail, setReferenceDetail] = useState<MemoryEntryDetail | null>(null);
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
  // Caso 0: lets non-effect handlers (open/close panel) re-fit the canvas to
  // the space left by the content panel, and reheat the sim to re-centre.
  const resizeRef = useRef<() => void>(() => {});
  // Caso 2: while searching, the graph recedes behind the results (the result
  // list is the focus). Read by the draw loop each frame (no re-subscribe).
  const recedeRef = useRef(false);
  function refitGraph() {
    resizeRef.current();
    alphaRef.current = Math.max(alphaRef.current, 0.6);
    // re-fit again after the panel's width transition settles
    setTimeout(() => { resizeRef.current(); alphaRef.current = Math.max(alphaRef.current, 0.5); }, 230);
  }
  // Hover preview (Obsidian page-preview): debounced popover with the
  // hovered node's rendered body. Refs drive the hot pointer path; only
  // `hoverPreview` is React state. Bodies cached per node id.
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hoverIdRef = useRef<string | null>(null);
  const hoverBodyCache = useRef<Map<string, string>>(new Map());
  const [hoverPreview, setHoverPreview] = useState<
    { node: MemoryGraphNode; x: number; y: number; body: string } | null
  >(null);

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
  // When navigating to a session from a provenance event, the timestamp of
  // that event — the messages tab scrolls to the nearest message (best-effort).
  const [sessionScrollTs, setSessionScrollTs] = useState<string | null>(null);
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

  // Navigate to the session a fact came from (provenance source_ref →
  // `session:<stem>` node). Selecting the node opens the session detail
  // panel via the existing `selected` effect. No-op when the session node
  // isn't in the graph payload (e.g. its .jsonl was removed).
  const selectSessionByStem = useCallback(
    (stem: string, targetTs?: string | null) => {
      const id = `session:${stem}`;
      const node =
        simNodesRef.current.find((n) => n.id === id) ??
        data?.nodes.find((n) => n.id === id);
      if (!node) return;
      setSelected(node as MemoryGraphNode);
      if (targetTs) {
        // Came from a provenance event: open the thread and scroll to the
        // moment that fact was recorded.
        setSessionTab("messages");
        setSessionScrollTs(targetTs);
      } else {
        setSessionTab("info");
      }
    },
    [data],
  );

  // Local-graph follows selection (Obsidian's local graph model): focusing
  // the active node dims the global hairball to its 1-hop neighbourhood, so
  // the graph recedes to a contextual map while the content takes the stage.
  // The panel's focus button still lets you unfocus to see the whole graph.
  useEffect(() => {
    setFocusRef(selected ? selected.id : null);
  }, [selected?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Caso 0: re-fit the graph whenever the content panel opens/closes/resizes —
  // the canvas shrinks to the leftover width and the sim reheats to re-centre.
  useEffect(() => {
    refitGraph();
  }, [selected?.id, !!referenceDetail, panelExpanded]); // eslint-disable-line react-hooks/exhaustive-deps

  // Switch the canvas to a node's ego-graph (uncapped neighbourhood). Used by
  // both graph clicks and search hits — so a searched node that the global
  // cap dropped is still brought in, centred, with just its relations.
  const focusOnNode = useCallback((ref: string) => {
    if (!tokenRef.current) return;
    void (async () => {
      try {
        const g = await fetchMemorySubgraph(tokenRef.current!, ref);
        setFocusGraph(g);
      } catch {
        /* ego fetch failed — stay on the current graph */
      }
    })();
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
  // Caso 2: the graph recedes when the search panel is up with a live query.
  const searching = searchOpen && (search.trim().length > 0 || searchResults != null);
  recedeRef.current = searching;

  // Edge popup state
  const [edgePopup, setEdgePopup] = useState<{
    x: number; y: number; detail: MemoryEdgeDetail | null; loading: boolean;
  } | null>(null);

  // Build simulation arrays from data
  const { simNodes, simEdges } = useMemo(() => {
    if (!data) return { simNodes: [] as SimNode[], simEdges: [] as SimEdge[] };
    const w = Math.max(80, (wrapRef.current?.clientWidth ?? 800) - reservedRightWidth());
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

  // Deduped, capped search results actually shown (skills already excluded
  // server-side via kinds="fact"). Shared by the header count and the list so
  // "N results" matches what's rendered.
  const searchDisplayed = useMemo(() => {
    const list = Array.isArray(searchResults?.results)
      ? searchResults!.results
      : [];
    const seen = new Set<string>();
    return list
      .filter((r) => {
        const base = r.uri.split("#")[0];
        if (seen.has(base)) return false;
        seen.add(base);
        return true;
      })
      .slice(0, 40);
  }, [searchResults]);

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
    // Defensive: a malformed payload (e.g. backend returned `{error}` with
    // no `results`) must NOT crash the whole view into a blank screen.
    if (!searchResults || !Array.isArray(searchResults.results)) return null;
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
      // Caso 0: the graph claims the space the content panel doesn't.
      // The open content panel (absolute, right) reserves width; the canvas
      // (a left-aligned flow child) shrinks to the remainder, so the graph
      // re-fits into its column instead of being covered by the overlay.
      const reserved = reservedRightWidth();
      const w = Math.max(80, wrap.clientWidth - reserved);
      const h = wrap.clientHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resizeRef.current = resize;
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
      // usable width = full minus the open content panel (Caso 0): centring
      // and bounds use the graph's actual column so it re-fits beside the panel.
      const w = Math.max(80, wrap.clientWidth - reservedRightWidth());
      const h = wrap.clientHeight;

      const alpha = alphaRef.current;
      if (alpha > 0.02) {
        tickForces(nodes, edges, w, h, alpha);
        alphaRef.current = alpha * 0.985;
      }

      ctx.clearRect(0, 0, w, h);

      // Caso 2: recede the whole graph behind the search results.
      ctx.globalAlpha = recedeRef.current ? 0.18 : 1;

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

      ctx.globalAlpha = 1;
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
        // Caso 1: click opens the node and focuses it IN PLACE (the global
        // graph dims to its 1-hop neighbourhood via focusRef). No ego-replace
        // and no camera move — the node you clicked is already on screen. The
        // ego-graph is reserved for off-cap nodes reached via search.
        setPanelExpanded(false);
        setActiveTab(hit.phantom ? "info" : "body");
        setReferenceDetail(null);
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
    [hitTestNode, hitTestEdge, focusOnNode],
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
        const node =
          hit && "weight" in hit && !("source" in hit) ? (hit as SimNode) : null;
        hoverRef.current = node;
        evt.currentTarget.style.cursor = hit ? "pointer" : "default";

        // Debounced hover preview for entity nodes (sessions excluded).
        const hid = node && node.type !== "session" ? node.id : null;
        if (hid !== hoverIdRef.current) {
          hoverIdRef.current = hid;
          if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
          if (!hid || !node) {
            setHoverPreview(null);
          } else {
            const px = x;
            const py = y;
            const hnode = node;
            hoverTimerRef.current = setTimeout(() => {
              const cached = hoverBodyCache.current.get(hid);
              if (cached !== undefined) {
                setHoverPreview({ node: hnode, x: px, y: py, body: cached });
                return;
              }
              if (!tokenRef.current) return;
              void fetchMemoryEntity(tokenRef.current, hid)
                .then((d) => {
                  const body = (d?.page?.body ?? "")
                    .replace(/<!--[\s\S]*?-->/g, "")
                    .trim()
                    .slice(0, 400);
                  hoverBodyCache.current.set(hid, body);
                  // Only show if still hovering the same node.
                  if (hoverIdRef.current === hid) {
                    setHoverPreview({ node: hnode, x: px, y: py, body });
                  }
                })
                .catch(() => {});
            }, 350);
          }
        }
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
          // kinds="fact" = everything EXCEPT skills (the proper backend
          // filter; "fact" is a value of `kinds`, not `scope`). Skills have
          // their own Skills view.
          const r = await searchMemoryApi(tokenRef.current, q, { kinds: "fact" });
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
        <h1 className="text-sm font-semibold">{t("memoryGraph.title")}</h1>
        {data ? (
          <span className="text-xs text-muted-foreground">
            {t("memoryGraph.stats", {
              nodesLabel: t("memoryGraph.nodesCount", { count: data.stats.node_count }),
              edgesLabel: t("memoryGraph.edgesCount", { count: data.stats.edge_count }),
            })}
            {data.stats.phantom_count > 0
              ? ` · ${t("memoryGraph.statsPhantom", { count: data.stats.phantom_count })}`
              : ""}
            {data.stats.truncated_nodes || data.stats.truncated_edges
              ? ` · ${t("memoryGraph.statsTruncated")}`
              : ""}
          </span>
        ) : null}
        {focusGraph ? (
          <button
            type="button"
            onClick={() => setFocusGraph(null)}
            className="rounded border border-border/40 px-2 py-0.5 text-[11px] text-primary hover:bg-muted"
          >
            ← {t("memoryGraph.backToFull")}
          </button>
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
              placeholder={t("memoryGraph.searchPlaceholder")}
              className={cn(
                "h-7 w-72 rounded-md border border-input bg-background pl-7 pr-2 text-[12.5px]",
                "outline-none focus:ring-1 focus:ring-ring",
              )}
            />
            {search ? (
              <button
                type="button"
                aria-label={t("memoryGraph.clear")}
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
              <Focus className="h-3 w-3" /> {t("memoryGraph.unfocus")}
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("memoryGraph.refresh")}
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
            {t("memoryGraph.loadingGraph")}
          </div>
        ) : null}
        {data && data.nodes.length === 0 ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-sm text-muted-foreground">
            <span>{t("memoryGraph.empty")}</span>
            <span className="text-xs">
              <Trans
                i18nKey="memoryGraph.emptyHint"
                components={{ code: <code className="rounded bg-muted px-1" /> }}
              />
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
            hoverIdRef.current = null;
            if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
            setHoverPreview(null);
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
                {t("memoryGraph.showAll")}
              </button>
            ) : null}
          </div>
        ) : null}

        {/* Search results panel (left side, slides over) */}
        {searchOpen && search.trim() ? (
          <aside className="absolute bottom-12 left-3 top-3 z-10 w-80 max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-lg border border-border/50 bg-card/95 shadow-lg backdrop-blur">
            <header className="flex items-center justify-between border-b border-border/40 px-3 py-2 text-xs">
              <span className="font-semibold">
                Search · {searchResults ? `${searchDisplayed.length} results` : "…"}
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
                  aria-label={t("memoryGraph.closeSearch")}
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            </header>
            <div className="max-h-full overflow-y-auto">
              {searchLoading ? (
                <div className="px-3 py-3 text-xs text-muted-foreground">{t("memoryGraph.searching")}</div>
              ) : null}
              {searchError ? (
                <div className="px-3 py-3 text-xs text-destructive">{searchError}</div>
              ) : null}
              {searchResults && searchResults.results.length === 0 && !searchLoading ? (
                <div className="px-3 py-3 text-xs text-muted-foreground">
                  {t("memoryGraph.noMatches")}
                </div>
              ) : null}
              {/* Deduped + skills-excluded list (see searchDisplayed memo). */}
              {searchDisplayed
                .map((r, idx) => {
                const isCanon = r.kind === "canonical";
                const id = isCanon ? r.uri.split("/").pop() ?? "" : r.uri;
                return (
                  <button
                    type="button"
                    key={`${r.uri}-${idx}`}
                    onClick={() => {
                      // Reference docs aren't graph nodes — open the content
                      // in the side panel. The search uri is
                      // `memory/reference/[reference:]<slug>`; normalise to the
                      // `reference:<slug>` form get_entry_detail expects.
                      if (r.class_name === "reference") {
                        const slug = r.uri
                          .replace(/^memory\/reference\//, "")
                          .replace(/^reference:/, "")
                          .split("#")[0];
                        setSelected(null);
                        if (tokenRef.current) {
                          void fetchMemoryEntry(
                            tokenRef.current,
                            `reference:${slug}`,
                          )
                            .then((d) => setReferenceDetail(d))
                            .catch(() => setReferenceDetail(null));
                        }
                        return;
                      }
                      // Pick the node to focus: a canonical hit IS an entity;
                      // a fragment (entry) points at the entities it tags —
                      // focus the first one so a fragment still drills into
                      // "the thing this is about".
                      const target = isCanon ? id : (r.entities ?? [])[0];
                      if (!target) return;
                      setReferenceDetail(null);
                      // Caso 1: ego-replace only for off-cap nodes (not in the
                      // current graph). In-graph hits just focus in place.
                      if (!simNodesRef.current.some((n) => n.id === target)) {
                        focusOnNode(target);
                      }
                      const node =
                        simNodesRef.current.find((n) => n.id === target) ?? {
                          id: target,
                          type: target.split(":")[0] || "unknown",
                          name: (isCanon ? r.headline || target : target).replace(
                            /^[a-z_]+:/,
                            "",
                          ),
                          weight: 0,
                          aliases: [],
                          phantom: false,
                        };
                      setSelected(node);
                      setActiveTab(node.phantom ? "info" : "body");
                    }}
                    className="block w-full border-t border-border/30 px-3 py-2.5 text-left text-[13px] hover:bg-muted/60"
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
                      <span className="truncate font-medium">
                        {(r.headline || r.uri).replace(/^[a-z_]+:/, "")}
                      </span>
                    </div>
                    {r.snippet ? (
                      <p className="mt-1 line-clamp-2 text-[12px] text-muted-foreground">
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
                aria-label={t("memoryGraph.closeEdge")}
              >
                <X className="h-3 w-3" />
              </Button>
            </div>
            {edgePopup.loading ? (
              <div className="text-muted-foreground">{t("memoryGraph.loading")}</div>
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

        {/* Right-side detail panel for the selected node. Obsidian-style
            content primacy: on select the content takes most of the surface
            (~58vw) and the graph recedes to a focused local view behind it;
            the maximize button widens further (~80vw) for long bodies. */}
        {selected ? (
          <aside
            className={cn(
              "mg-cpanel absolute right-3 top-3 z-10 flex max-w-[calc(100vw-1.5rem)] flex-col rounded-lg border border-border/50 bg-card/95 text-sm shadow-lg backdrop-blur",
              "transition-[width] duration-200 ease-out",
              panelExpanded ? "w-[calc(100%-1.5rem)]" : "w-[22rem]",
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
                aria-label={focusRef === selected.id ? t("memoryGraph.unfocus") : t("memoryGraph.focusOneHop")}
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
                aria-label={t("memoryGraph.close")}
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
                      { id: "info", label: t("memoryGraph.tabInfo") },
                      { id: "messages", label: `Messages${sessionDetail?.recent_messages.length ? ` (${sessionDetail.recent_messages.length})` : ""}` },
                      { id: "events", label: `Events${sessionDetail?.events.length ? ` (${sessionDetail.events.length})` : ""}` },
                      { id: "memory_ops", label: `Memory ops${sessionDetail?.memory_ops.length ? ` (${sessionDetail.memory_ops.length})` : ""}` },
                      { id: "entries", label: `Entries${sessionDetail?.entries_linked.length ? ` (${sessionDetail.entries_linked.length})` : ""}` },
                    ] as const
                  ).map((tab) => (
                    <button
                      key={tab.id}
                      type="button"
                      onClick={() => setSessionTab(tab.id as SessionTabName)}
                      className={cn(
                        "rounded px-2 py-1 font-medium transition-colors",
                        sessionTab === tab.id
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground hover:bg-muted",
                      )}
                    >
                      {tab.label}
                    </button>
                  ))
                : (
                    [
                      { id: "info", label: t("memoryGraph.tabInfo") },
                      { id: "body", label: t("memoryGraph.tabBody") },
                      { id: "entries", label: t("memoryGraph.tabEntries") },
                      { id: "provenance", label: `${t("memoryGraph.provenance")}${detail?.provenance.length ? ` (${detail.provenance.length})` : ""}` },
                      { id: "history", label: `History${detail?.history.length ? ` (${detail.history.length})` : ""}` },
                      { id: "sources", label: `Sources${detail?.entries.length ? ` (${detail.entries.length})` : ""}` },
                      { id: "archive", label: `Archive${detail?.archive.length ? ` (${detail.archive.length})` : ""}` },
                    ] as const
                  )
                    // policy (a): phantom nodes have no consolidated page, so
                    // "Body" and "History" are structurally always empty —
                    // hide them instead of showing dead tabs.
                    .filter((tab) =>
                      !selected.phantom || (tab.id !== "body" && tab.id !== "history"),
                    )
                    // "Procedencia" only when there are provenance events.
                    .filter(
                      (tab) =>
                        tab.id !== "provenance" ||
                        (detail?.provenance.length ?? 0) > 0,
                    )
                    .map((tab) => (
                    <button
                      key={tab.id}
                      type="button"
                      onClick={() => setActiveTab(tab.id as TabName)}
                      className={cn(
                        "rounded px-2 py-1 font-medium transition-colors",
                        activeTab === tab.id
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground hover:bg-muted",
                      )}
                    >
                      {tab.label}
                    </button>
                  ))}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2 text-xs">
              {detailLoading ? (
                <div className="text-muted-foreground">{t("memoryGraph.loadingDetail")}</div>
              ) : null}
              {detailError ? (
                <div className="text-destructive">{detailError}</div>
              ) : null}
              {!detail && !sessionDetail && !detailLoading && selected.phantom ? (
                <p className="text-[11px] text-muted-foreground">
                  <Trans
                    i18nKey="memoryGraph.noConsolidatedHint"
                    components={{ code: <code className="rounded bg-muted px-1" /> }}
                  />
                </p>
              ) : null}
              {sessionDetail ? (
                <SessionTabs
                  detail={sessionDetail}
                  tab={sessionTab}
                  scrollTs={sessionScrollTs}
                />
              ) : null}
              {detail ? (
                <>
                  {activeTab === "info" ? (
                    <dl className="space-y-2">
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">{t("memoryGraph.fieldType")}</dt>
                        <dd className="font-mono">{detail.page?.type ?? selected.type}</dd>
                      </div>
                      <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">
                          {t("memoryGraph.entriesReferencing")}
                        </dt>
                        <dd className="font-mono">{selected.weight}</dd>
                      </div>
                      {!detail.page ? (
                        <PhantomInfo
                          entries={detail.entries}
                          selectedRef={selected.id}
                          nodes={data?.nodes ?? []}
                          onSelect={setSelected}
                        />
                      ) : null}
                      {detail.page && detail.page.dream_processed_through ? (
                        <div className="flex justify-between gap-2">
                          <dt className="text-muted-foreground">{t("memoryGraph.fieldLastDreamed")}</dt>
                          <dd className="font-mono text-[11px]">
                            {detail.page.dream_processed_through}
                          </dd>
                        </div>
                      ) : null}
                      {detail.page && detail.page.aliases.length > 0 ? (
                        <div>
                          <dt className="text-muted-foreground">{t("memoryGraph.fieldAliases")}</dt>
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
                      {detail.page && detail.page.identifiers ? (
                        <div>
                          <dt className="text-muted-foreground">{t("memoryGraph.fieldIdentifiers")}</dt>
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
                    detail.page && detail.page.body ? (
                      <MarkdownTextRenderer className="text-[12.5px] leading-relaxed">
                        {/* Strip the inline `<!-- author [[sessions/…]] -->`
                            provenance markers — they're machine metadata, not
                            content, and render as raw noise. */}
                        {detail.page.body.replace(/<!--[\s\S]*?-->/g, "").trim()}
                      </MarkdownTextRenderer>
                    ) : (
                      <p className="text-muted-foreground">{t("memoryGraph.noBody")}</p>
                    )
                  ) : null}

                  {activeTab === "provenance" ? (
                    detail.provenance.length === 0 ? (
                      <p className="text-muted-foreground">
                        {t("memoryGraph.noProvenance")}
                      </p>
                    ) : (
                      <div>
                        {detail.page?.created_at ? (
                          <p className="mb-2 text-[11px] text-muted-foreground">
                            {t("memoryGraph.provCreated")}{" "}
                            {detail.page.created_at.slice(0, 10)}
                            {detail.page.author ? ` · ${detail.page.author}` : ""}
                          </p>
                        ) : null}
                        <ul className="space-y-1.5">
                          {detail.provenance.map((ev, i) => {
                            const sessionInGraph =
                              ev.session_stem != null &&
                              (data?.nodes.some(
                                (n) => n.id === `session:${ev.session_stem}`,
                              ) ?? false);
                            return (
                              <li
                                key={i}
                                className="rounded border border-border/40 bg-background/60 p-2"
                              >
                                <div className="flex items-center justify-between text-[10.5px] text-muted-foreground">
                                  <span>
                                    {ev.kind === "relation"
                                      ? t("memoryGraph.provRelation")
                                      : t("memoryGraph.provAttribute")}
                                    {ev.author ? ` · ${ev.author}` : ""}
                                  </span>
                                  <span>{ev.when ? ev.when.slice(0, 10) : ""}</span>
                                </div>
                                {ev.detail ? (
                                  <div className="mt-0.5 font-mono text-[11px] break-all">
                                    {ev.detail}
                                  </div>
                                ) : null}
                                {sessionInGraph ? (
                                  <button
                                    type="button"
                                    onClick={() =>
                                      selectSessionByStem(ev.session_stem!, ev.when)
                                    }
                                    className="mt-1 text-[10.5px] text-primary hover:underline"
                                  >
                                    {t("memoryGraph.provFromSession")}
                                    {ev.turn != null
                                      ? ` · ${t("memoryGraph.provTurn", { turn: ev.turn })}`
                                      : ""}{" "}
                                    →
                                  </button>
                                ) : ev.source_ref ? (
                                  <div className="mt-1 text-[10.5px] text-muted-foreground break-all">
                                    {ev.source_ref}
                                  </div>
                                ) : null}
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                    )
                  ) : null}

                  {activeTab === "history" ? (
                    detail.history.length === 0 ? (
                      <p className="text-muted-foreground">
                        {t("memoryGraph.noHistory")}
                      </p>
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

                  {activeTab === "entries" ? (
                    <EntriesTab
                      token={tokenRef.current ?? ""}
                      entityRef={selected.id}
                    />
                  ) : null}

                  {activeTab === "archive" ? (
                    detail.archive.length === 0 ? (
                      <p className="text-muted-foreground">
                        {t("memoryGraph.noAbsorptions")}
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
              {t("memoryGraph.interactionHint")}
            </footer>
          </aside>
        ) : null}

        {/* Reference content panel — references aren't graph nodes, so a
            reference search hit opens its rendered doc here. */}
        {referenceDetail && !selected ? (
          <aside
            className="mg-cpanel absolute right-3 top-3 z-10 flex w-[min(58vw,44rem)] max-w-[calc(100vw-1.5rem)] flex-col rounded-lg border border-border/50 bg-card/95 text-sm shadow-lg backdrop-blur"
            style={{ maxHeight: "calc(100% - 1.5rem)" }}
          >
            <header className="flex items-start gap-2 border-b border-border/40 px-3 py-2">
              <span className="mt-1 inline-block h-2.5 w-2.5 shrink-0 rounded-full bg-amber-500/70" />
              <div className="min-w-0 flex-1">
                <div className="truncate font-semibold">
                  {referenceDetail.frontmatter.headline}
                </div>
                <div className="truncate text-xs text-muted-foreground">
                  reference
                  {referenceDetail.frontmatter.valid_from
                    ? ` · ${referenceDetail.frontmatter.valid_from.slice(0, 10)}`
                    : ""}
                </div>
              </div>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("memoryGraph.close")}
                onClick={() => setReferenceDetail(null)}
                className="h-6 w-6"
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </header>
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
              {referenceDetail.body ? (
                <MarkdownTextRenderer className="text-[12.5px] leading-relaxed">
                  {referenceDetail.body.replace(/<!--[\s\S]*?-->/g, "").trim()}
                </MarkdownTextRenderer>
              ) : (
                <p className="text-muted-foreground">{t("memoryGraph.noBody")}</p>
              )}
            </div>
          </aside>
        ) : null}

        {/* Hover preview (Obsidian page-preview): non-interactive popover with
            the hovered node's rendered body snippet. */}
        {hoverPreview && hoverPreview.node.id !== selected?.id ? (
          <div
            className="pointer-events-none absolute z-30 w-72 max-w-[calc(100vw-1.5rem)] rounded-lg border border-border/50 bg-card/95 p-2.5 text-xs shadow-lg backdrop-blur"
            style={{
              left: clamp(hoverPreview.x + 14, 8, (wrapRef.current?.clientWidth ?? 800) - 300),
              top: clamp(hoverPreview.y + 14, 8, (wrapRef.current?.clientHeight ?? 600) - 200),
            }}
          >
            <div className="mb-1 flex items-center gap-1.5">
              <span
                className="inline-block h-2 w-2 shrink-0 rounded-full"
                style={{ background: colorForType(hoverPreview.node.type) }}
              />
              <span className="truncate font-semibold">{hoverPreview.node.name}</span>
            </div>
            {hoverPreview.body ? (
              <p className="line-clamp-6 whitespace-pre-wrap text-[11px] leading-relaxed text-muted-foreground">
                {hoverPreview.body}
              </p>
            ) : (
              <p className="text-[11px] text-muted-foreground">
                {hoverPreview.node.type}
                {hoverPreview.node.phantom ? " · phantom" : ""}
              </p>
            )}
          </div>
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
  scrollTs,
}: {
  detail: MemorySessionDetail;
  tab: SessionTabName;
  scrollTs?: string | null;
}) {
  const { t } = useTranslation();
  const listRef = useRef<HTMLUListElement | null>(null);
  const lastScrolledRef = useRef<string | null>(null);
  const [highlightIdx, setHighlightIdx] = useState<number | null>(null);

  // Best-effort: index of the message nearest `scrollTs` (a provenance
  // event's UTC timestamp). Message ts are naive/local; the diff aligns when
  // the session was written in the viewer's timezone — hence "best-effort".
  const targetIdx = useMemo(() => {
    if (!scrollTs) return null;
    const target = new Date(scrollTs).getTime();
    if (Number.isNaN(target)) return null;
    let best = -1;
    let bestDiff = Infinity;
    detail.recent_messages.forEach((m, i) => {
      if (m.ts == null) return;
      const ms =
        typeof m.ts === "number"
          ? m.ts * 1000
          : new Date(String(m.ts)).getTime();
      if (Number.isNaN(ms)) return;
      const diff = Math.abs(ms - target);
      if (diff < bestDiff) {
        bestDiff = diff;
        best = i;
      }
    });
    return best >= 0 ? best : null;
  }, [scrollTs, detail.recent_messages]);

  useEffect(() => {
    if (tab !== "messages" || !scrollTs || targetIdx == null) return;
    if (lastScrolledRef.current === scrollTs) return;
    const el = listRef.current?.querySelector(
      `[data-msg-idx="${targetIdx}"]`,
    ) as HTMLElement | null;
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    lastScrolledRef.current = scrollTs;
    setHighlightIdx(targetIdx);
    const tmr = setTimeout(() => setHighlightIdx(null), 2500);
    return () => clearTimeout(tmr);
  }, [tab, scrollTs, targetIdx]);

  if (tab === "info") {
    const info = detail.info;
    const metaEnts = detail.entities_tagged.from_meta;
    const refEnts = detail.entities_tagged.from_source_refs;
    return (
      <dl className="space-y-2">
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">{t("memoryGraph.fieldSessionKey")}</dt>
          <dd className="font-mono">{detail.session_key ?? detail.session_ref}</dd>
        </div>
        {info.channel ? (
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">{t("memoryGraph.fieldChannel")}</dt>
            <dd className="font-mono">{info.channel}</dd>
          </div>
        ) : null}
        {info.model ? (
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">{t("memoryGraph.fieldModel")}</dt>
            <dd className="font-mono">{info.model}</dd>
          </div>
        ) : null}
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">{t("memoryGraph.fieldMessages")}</dt>
          <dd className="font-mono">{info.message_count}</dd>
        </div>
        {info.created_at ? (
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">{t("memoryGraph.fieldCreated")}</dt>
            <dd className="font-mono text-[11px]">{info.created_at.slice(0, 19)}</dd>
          </div>
        ) : null}
        {info.updated_at ? (
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">{t("memoryGraph.fieldUpdated")}</dt>
            <dd className="font-mono text-[11px]">{info.updated_at.slice(0, 19)}</dd>
          </div>
        ) : null}
        {metaEnts.length > 0 ? (
          <div>
            <dt className="text-muted-foreground">{t("memoryGraph.entitiesFromMeta")}</dt>
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
            <dt className="text-muted-foreground">{t("memoryGraph.entitiesFromSources")}</dt>
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
      return <p className="text-muted-foreground">{t("memoryGraph.noRecentMessages")}</p>;
    }
    return (
      <ul className="space-y-2" ref={listRef}>
        {detail.recent_messages.map((m, i) => (
          <li
            key={i}
            data-msg-idx={i}
            className={cn(
              "rounded border p-2 transition-colors",
              highlightIdx === i
                ? "border-primary/60 bg-primary/10"
                : "border-border/40 bg-background/60",
            )}
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
      return <p className="text-muted-foreground">{t("memoryGraph.noLifecycleEvents")}</p>;
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
        {t("memoryGraph.noSourceLinkedEntries")}
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


// ---------------------------------------------------------------------------
// PhantomInfo — Info-tab body for a phantom entity (tagged in entries but not
// yet consolidated into a page). Answers "what is this?" from the referencing
// entries: a provisional description (the most representative entry) + the
// entities it co-occurs with (clickable to navigate). No page-derived fields.
// ---------------------------------------------------------------------------

function PhantomInfo({
  entries,
  selectedRef,
  nodes,
  onSelect,
}: {
  entries: MemoryEntityDetail["entries"];
  selectedRef: string;
  nodes: MemoryGraphNode[];
  onSelect: (node: MemoryGraphNode) => void;
}) {
  const { t } = useTranslation();
  // Provisional description: prefer a durable `stable` entry, else newest.
  const best = entries.find((e) => e.class === "stable") ?? entries[0];
  const byClass = new Map<string, number>();
  const coCounts = new Map<string, number>();
  for (const e of entries) {
    byClass.set(e.class, (byClass.get(e.class) ?? 0) + 1);
    for (const ref of e.entities ?? []) {
      if (ref === selectedRef) continue;
      coCounts.set(ref, (coCounts.get(ref) ?? 0) + 1);
    }
  }
  const breakdown = [...byClass.entries()].map(([c, n]) => `${n} ${c}`).join(" · ");
  const coMentions = [...coCounts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
  const nodeByRef = new Map(nodes.map((n) => [n.id, n]));
  const chipLabel = (ref: string) =>
    ref.includes(":") ? ref.split(":").slice(1).join(":") : ref;

  return (
    <>
      {breakdown ? (
        <div className="text-[11px] text-muted-foreground">{breakdown}</div>
      ) : null}
      {best ? (
        <div>
          <dt className="text-muted-foreground">{t("memoryGraph.phantomWhatIs")}</dt>
          <dd className="mt-0.5">
            {best.headline ? <div className="font-medium">{best.headline}</div> : null}
            {best.body ? (
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                {best.body.slice(0, 200)}
                {best.body.length > 200 ? "…" : ""}
              </p>
            ) : null}
          </dd>
        </div>
      ) : null}
      {coMentions.length > 0 ? (
        <div>
          <dt className="text-muted-foreground">{t("memoryGraph.coMentions")}</dt>
          <dd className="mt-0.5 flex flex-wrap gap-1">
            {coMentions.map(([ref, count]) => {
              const node = nodeByRef.get(ref);
              return node ? (
                <button
                  key={ref}
                  type="button"
                  title={ref}
                  onClick={() => onSelect(node)}
                  className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10.5px] transition-colors hover:bg-accent hover:text-accent-foreground"
                >
                  {chipLabel(ref)}{" "}
                  <span className="text-muted-foreground">×{count}</span>
                </button>
              ) : (
                <span
                  key={ref}
                  title={ref}
                  className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10.5px] opacity-60"
                >
                  {chipLabel(ref)}{" "}
                  <span className="text-muted-foreground">×{count}</span>
                </span>
              );
            })}
          </dd>
        </div>
      ) : null}
      <p className="text-[11px] text-muted-foreground">
        <Trans
          i18nKey="memoryGraph.noConsolidatedHint"
          components={{ code: <code className="rounded bg-muted px-1" /> }}
        />
      </p>
    </>
  );
}


// ---------------------------------------------------------------------------
// EntriesTab (P12) — browse + read + archive memory entries that reference
// the currently-selected entity. Each row is clickable; the active row's
// frontmatter + body + backlinks render in a sub-panel below the list.
// Wikilinks in the body navigate within the tab (replace the sub-panel).
// ---------------------------------------------------------------------------


function EntriesTab({
  token,
  entityRef,
}: {
  token: string;
  entityRef: string;
}) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<MemorySearchResult[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selectedUri, setSelectedUri] = useState<string | null>(null);
  const [detail, setDetail] = useState<MemoryEntryDetail | null>(null);
  const [backlinks, setBacklinks] = useState<MemoryBacklinksPayload | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [archiving, setArchiving] = useState(false);

  // Load the row list whenever the selected entity changes. We use
  // ``searchMemoryApi`` with the entity ref as the query — the FTS
  // index matches the ref in frontmatter, so the result set IS the
  // entries that tag this entity. We then drop:
  //  - canonical entity_page rows (those have their own tabs)
  //  - session rows (those have their own session view)
  //  - any class outside the forgettable set (only those have a
  //    detail endpoint + Archive button here)
  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setListError(null);
    setSelectedUri(null);
    setDetail(null);
    setBacklinks(null);
    if (!token || !entityRef) return;
    void (async () => {
      try {
        const r: MemorySearchPayload = await searchMemoryApi(token, entityRef);
        if (cancelled) return;
        const forgettable = new Set([
          "episodic", "stable", "corpus", "session_summary",
        ]);
        const filtered = r.results.filter((res) =>
          forgettable.has(res.class_name ?? "")
          && res.uri.startsWith("memory/"),
        );
        setRows(filtered);
      } catch (err) {
        if (cancelled) return;
        setListError(
          err instanceof ApiError ? `HTTP ${err.status}` : (err as Error).message,
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, entityRef]);

  // Load detail + backlinks when the user opens a row.
  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setBacklinks(null);
    setConfirmArchive(false);
    setDetailError(null);
    if (!token || !selectedUri) {
      setDetailLoading(false);
      return;
    }
    setDetailLoading(true);
    void (async () => {
      try {
        const [d, bl] = await Promise.all([
          fetchMemoryEntry(token, selectedUri),
          fetchMemoryBacklinks(token, selectedUri),
        ]);
        if (cancelled) return;
        if (d === null) {
          setDetailError(t("memoryGraph.entries.notFound"));
        } else {
          setDetail(d);
          setBacklinks(bl);
        }
      } catch (err) {
        if (cancelled) return;
        setDetailError(
          err instanceof ApiError ? `HTTP ${err.status}` : (err as Error).message,
        );
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, selectedUri, t]);

  const onArchive = useCallback(async () => {
    if (!detail) return;
    setArchiving(true);
    try {
      const out = await forgetMemoryEntry(token, detail.uri);
      if (out.result === "archived") {
        // Drop the row from the list, clear the sub-panel.
        setRows((prev) => prev?.filter((r) => r.uri !== detail.uri) ?? null);
        setSelectedUri(null);
        setDetail(null);
        setBacklinks(null);
      } else if (out.result === "protected") {
        setDetailError(t("memoryGraph.entries.cantArchiveProtected"));
      } else if (out.result === "not_found") {
        // Stale — drop from list anyway.
        setRows((prev) => prev?.filter((r) => r.uri !== detail.uri) ?? null);
        setSelectedUri(null);
      } else {
        setDetailError(out.detail || out.result);
      }
    } catch (err) {
      setDetailError(
        err instanceof ApiError ? `HTTP ${err.status}` : (err as Error).message,
      );
    } finally {
      setArchiving(false);
      setConfirmArchive(false);
    }
  }, [detail, token, t]);

  if (listError) {
    return <p className="text-destructive">{listError}</p>;
  }
  if (rows === null) {
    return <p className="text-muted-foreground">{t("memoryGraph.loadingDetail")}</p>;
  }
  if (rows.length === 0 && !selectedUri) {
    return <p className="text-muted-foreground">{t("memoryGraph.entries.empty")}</p>;
  }

  // Detail sub-panel takes over the tab when a row is open.
  if (selectedUri) {
    return (
      <div className="space-y-2">
        <button
          type="button"
          onClick={() => setSelectedUri(null)}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted"
        >
          <ChevronLeft className="h-3 w-3" aria-hidden />
          {t("memoryGraph.entries.backToList")}
        </button>
        {detailLoading ? (
          <p className="text-muted-foreground">{t("memoryGraph.loadingDetail")}</p>
        ) : null}
        {detailError ? (
          <p className="text-destructive">{detailError}</p>
        ) : null}
        {detail ? (
          <div className="space-y-3">
            <div className="rounded border border-border/40 bg-background/60 p-2">
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] uppercase">
                  {detail.class_name}
                </span>
                <span className="font-mono text-[10px] text-muted-foreground">
                  {detail.uri}
                </span>
              </div>
              <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-[11px]">
                <dt className="text-muted-foreground">{t("memoryGraph.entries.headline")}</dt>
                <dd className="font-medium">{detail.frontmatter.headline}</dd>
                {detail.frontmatter.valid_from ? (
                  <>
                    <dt className="text-muted-foreground">{t("memoryGraph.entries.validFrom")}</dt>
                    <dd className="font-mono">{detail.frontmatter.valid_from}</dd>
                  </>
                ) : null}
                {detail.frontmatter.author ? (
                  <>
                    <dt className="text-muted-foreground">{t("memoryGraph.entries.author")}</dt>
                    <dd className="font-mono">{detail.frontmatter.author}</dd>
                  </>
                ) : null}
                {detail.frontmatter.entities.length > 0 ? (
                  <>
                    <dt className="text-muted-foreground">{t("memoryGraph.entries.entitiesField")}</dt>
                    <dd className="font-mono">
                      {detail.frontmatter.entities.join(", ")}
                    </dd>
                  </>
                ) : null}
                {detail.frontmatter.source_refs.length > 0 ? (
                  <>
                    <dt className="text-muted-foreground">{t("memoryGraph.entries.sourceRefs")}</dt>
                    <dd className="space-y-0.5 break-all font-mono text-[10.5px]">
                      {detail.frontmatter.source_refs.map((s, i) => (
                        <div key={`${s}-${i}`}>{s}</div>
                      ))}
                    </dd>
                  </>
                ) : null}
                {detail.frontmatter.related.length > 0 ? (
                  <>
                    <dt className="text-muted-foreground">{t("memoryGraph.entries.related")}</dt>
                    <dd className="space-y-0.5 break-all font-mono text-[10.5px]">
                      {detail.frontmatter.related.map((s, i) => (
                        <div key={`${s}-${i}`}>{s}</div>
                      ))}
                    </dd>
                  </>
                ) : null}
              </dl>
            </div>

            {detail.body ? (
              <div className="rounded border border-border/40 bg-background/60 p-2">
                <div className="mb-1 text-[10.5px] uppercase tracking-wide text-muted-foreground">
                  {t("memoryGraph.entries.body")}
                </div>
                <MarkdownTextRenderer
                  onWikiLinkClick={(target) => {
                    // Replace the sub-panel with the linked entry.
                    // Only intercept memory/<class>/<id> targets; ignore
                    // wikilinks pointing to entity pages or anything else.
                    if (/^memory\/(episodic|stable|corpus|session_summary)\//.test(target)) {
                      setSelectedUri(target);
                    }
                  }}
                  className="text-[11.5px]"
                >
                  {detail.body}
                </MarkdownTextRenderer>
              </div>
            ) : null}

            {backlinks ? (
              <div className="rounded border border-border/40 bg-background/60 p-2">
                <div className="mb-1 text-[10.5px] uppercase tracking-wide text-muted-foreground">
                  {t("memoryGraph.entries.backlinks")}
                  {backlinks.truncated ? ` (${t("memoryGraph.entries.truncated")})` : ""}
                </div>
                {backlinks.backlinks.length === 0 ? (
                  <p className="text-[11px] text-muted-foreground">
                    {t("memoryGraph.entries.noBacklinks")}
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {backlinks.backlinks.map((bl) => (
                      <li key={bl.uri}>
                        <button
                          type="button"
                          onClick={() => setSelectedUri(bl.uri)}
                          className="w-full rounded px-1 py-0.5 text-left text-[11px] text-primary hover:bg-muted"
                        >
                          <span className="font-mono text-[10px] text-muted-foreground">
                            [{bl.context}]
                          </span>{" "}
                          {bl.headline || bl.uri}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ) : null}

            <div className="flex items-center justify-end gap-2 pt-1">
              {confirmArchive ? (
                <>
                  <span className="text-[11px] text-muted-foreground">
                    {t("memoryGraph.entries.confirmArchive")}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={archiving}
                    onClick={() => setConfirmArchive(false)}
                    className="rounded-full"
                  >
                    {t("memoryGraph.entries.cancel")}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={archiving}
                    onClick={() => void onArchive()}
                    className="rounded-full text-destructive hover:text-destructive"
                  >
                    <Trash2 className="mr-1 h-3 w-3" aria-hidden />
                    {archiving
                      ? t("memoryGraph.entries.archiving")
                      : t("memoryGraph.entries.confirm")}
                  </Button>
                </>
              ) : (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setConfirmArchive(true)}
                  className="rounded-full text-muted-foreground hover:text-destructive"
                >
                  <Trash2 className="mr-1 h-3 w-3" aria-hidden />
                  {t("memoryGraph.entries.archive")}
                </Button>
              )}
            </div>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <ul className="space-y-1.5">
      {rows.map((r) => (
        <li key={r.uri}>
          <button
            type="button"
            onClick={() => setSelectedUri(r.uri)}
            className="block w-full rounded border border-border/40 bg-background/60 p-2 text-left hover:border-border/80"
          >
            <div className="flex items-center justify-between gap-2 text-[10.5px] text-muted-foreground">
              <span className="rounded bg-muted px-1 font-mono uppercase">
                {r.class_name ?? r.kind}
              </span>
              {r.valid_from ? (
                <span className="font-mono">{r.valid_from.slice(0, 10)}</span>
              ) : null}
            </div>
            {r.headline ? (
              <div className="mt-0.5 text-[11.5px] font-medium">{r.headline}</div>
            ) : null}
            {r.snippet ? (
              <div className="mt-0.5 text-[10.5px] text-muted-foreground line-clamp-2">
                {r.snippet}
              </div>
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  );
}
