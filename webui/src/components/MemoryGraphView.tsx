import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import {
  ArrowDownUp,
  BookOpen,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Focus,
  LayoutGrid,
  Maximize2,
  Minimize2,
  Network,
  RefreshCw,
  Scan,
  Search as SearchIcon,
  Table2,
  Trash2,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { DocumentsShelf } from "@/components/DocumentsShelf";
import { useGraphLayers } from "@/hooks/useGraphLayers";
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
  forgetMemoryEntry,
  searchMemoryApi,
  type MemoryBacklinksPayload,
  type MemoryEdgeDetail,
  type MemoryEntityDetail,
  type MemoryEntryDetail,
  type MemoryGraphNode,
  type MemorySearchPayload,
  type MemorySearchResult,
  type MemorySessionDetail,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  colorForType,
  type EntitySortKey,
} from "@/lib/memory-graph-style";
import {
  labelBudget,
  overviewToGraph,
  radiusForBubble,
  radiusForNode,
  visibleLabels,
  type LabelCandidate,
} from "@/lib/memory-graph-layout";
import { readCanvasTheme, watchTheme, type CanvasTheme } from "@/lib/canvas-theme";
import { MemoryEntityCards } from "@/components/MemoryEntityCards";
import { MemoryEntityTable } from "@/components/MemoryEntityTable";
import { MemoryTypeFilter } from "@/components/MemoryTypeFilter";

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
  // Overview-layer-only fields (see OverviewNode in memory-graph-layout.ts):
  // a bubble stands in for a whole cluster, `count` is its member count.
  // Optional because normal (non-overview) nodes never carry them.
  kind?: "bubble";
  count?: number;
}

interface SimEdge {
  source: SimNode;
  target: SimNode;
  weight: number;
}

/**
 * Strip HTML comment markers (provenance metadata) from memory body text.
 * Applies the removal repeatedly until the string is stable so that nested or
 * overlapping ``<!-- -->`` sequences cannot leave a dangling ``<!--`` behind
 * (a single pass over multi-character delimiters is not sufficient).
 */
function stripHtmlComments(text: string): string {
  let prev: string;
  let out = text;
  do {
    prev = out;
    out = out.replace(/<!--[\s\S]*?-->/g, "");
  } while (out !== prev);
  return out;
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

// Bubbles size by member count (falling back to weight if count is somehow
// absent); every other node (including sessions, handled inside
// radiusForNode) sizes by weight/type.
function nodeRadius(n: SimNode): number {
  return n.kind === "bubble"
    ? radiusForBubble(n.count ?? n.weight)
    : radiusForNode(n.weight, n.type);
}

// prefers-reduced-motion is read once per RAF-effect mount rather than once
// per animation frame — the check is cheap, but there is no reason to poll
// it 60x/sec, and a fresh read per mount still picks up an OS setting change
// made while the graph view was unmounted.
function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
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
  // Position integration + boundary handling.
  //
  // A plain clamp pins a node to the wall but keeps its outward velocity, and
  // every force above scales with `alpha` (which cools toward 0) — so a node
  // flung into a corner during the initial high-energy layout would freeze
  // there, unreachable by the (now ~0) centring force. Two corrections keep the
  // periphery clean without touching the central layout: on contact, drop the
  // outward velocity so the node stops pressing into the wall; and within a
  // boundary band apply a small alpha-INDEPENDENT inward pull, so disconnected /
  // low-degree nodes are always reclaimed toward the canvas rather than piling
  // in the corners.
  const margin = 20;
  const band = 90;
  const nudgeX = width > 2 * (margin + band);
  const nudgeY = height > 2 * (margin + band);
  for (const n of nodes) {
    if (n.pinned) continue;
    n.x += n.vx;
    n.y += n.vy;
    if (n.x < margin) { n.x = margin; if (n.vx < 0) n.vx = 0; }
    else if (n.x > width - margin) { n.x = width - margin; if (n.vx > 0) n.vx = 0; }
    if (n.y < margin) { n.y = margin; if (n.vy < 0) n.vy = 0; }
    else if (n.y > height - margin) { n.y = height - margin; if (n.vy > 0) n.vy = 0; }
    if (nudgeX) {
      const lo = margin + band;
      const hi = width - margin - band;
      if (n.x < lo) n.x += (lo - n.x) * 0.05;
      else if (n.x > hi) n.x -= (n.x - hi) * 0.05;
    }
    if (nudgeY) {
      const lo = margin + band;
      const hi = height - margin - band;
      if (n.y < lo) n.y += (lo - n.y) * 0.05;
      else if (n.y > hi) n.y -= (n.y - hi) * 0.05;
    }
  }
}

type TabName = "info" | "body" | "provenance" | "history" | "sources" | "archive" | "entries";
type SessionTabName = "info" | "messages" | "events" | "memory_ops" | "entries";

export function MemoryGraphView(_props: MemoryGraphViewProps) {
  const { t } = useTranslation();
  const { data: rawData, loading, error, refresh } = useMemoryGraph(_props.active);
  // Reference docs (memory/references/*) aren't graph nodes; clicking a
  // reference search hit opens its content in this side panel.
  const [referenceDetail, setReferenceDetail] = useState<MemoryEntryDetail | null>(null);
  const { token } = useClient();
  const tokenRef = useRef(token);
  tokenRef.current = token;
  // Two content domains under one memory page: the entity knowledge graph and
  // the Library shelf of ingested reference documents. Presentation of the
  // entities (graph canvas / cards grid / table) is a separate axis below —
  // three views of the same set, not sibling tabs (the Obsidian Bases model).
  const [mode, setMode] = useState<"entities" | "documents">("entities");
  // Entities presentation. Persisted; the graph canvas is near-useless on
  // touch, so compact viewports fall back to cards (see effectiveView).
  const [view, setView] = useState<"graph" | "cards" | "table">(() => {
    try {
      const stored = localStorage.getItem("durin.memoryGraph.view");
      if (stored === "graph" || stored === "cards" || stored === "table") {
        return stored;
      }
    } catch {
      /* localStorage unavailable */
    }
    return "graph";
  });
  const setViewPersisted = useCallback((v: "graph" | "cards" | "table") => {
    setView(v);
    try {
      localStorage.setItem("durin.memoryGraph.view", v);
    } catch {
      /* localStorage unavailable: ephemeral choice is fine */
    }
  }, []);
  // Shared ordering for the cards/table presentations (the graph's layout is
  // force-directed — sort does not apply there).
  const [sortKey, setSortKey] = useState<EntitySortKey>("recent");

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const simNodesRef = useRef<SimNode[]>([]);
  const simEdgesRef = useRef<SimEdge[]>([]);
  const alphaRef = useRef(1);
  const rafRef = useRef<number | null>(null);
  const draggingRef = useRef<SimNode | null>(null);
  // Overview layer only: a press on a bubble/hub/loose node starts a drag
  // (see onPointerDown) rather than navigating immediately, so the click-vs-
  // drag decision is made on release — screen coords of the press, compared
  // against release position, distinguish "drilled in" from "repositioned".
  const overviewPressRef = useRef<{ id: string; x: number; y: number } | null>(
    null,
  );
  const hoverRef = useRef<SimNode | null>(null);
  // Camera over the sim's world coordinates: screen = world * k + (tx, ty).
  // Auto-fit continuously frames the visible nodes (smoothed each frame,
  // zoom-in clamped) until the user takes manual control via wheel zoom or
  // background pan; the floating fit button re-engages it. Refs drive the
  // hot pointer/draw paths; `autoFit` state only mirrors the mode for UI.
  const cameraRef = useRef({ k: 1, tx: 0, ty: 0 });
  const autoFitRef = useRef(true);
  const [autoFit, setAutoFit] = useState(true);
  const panRef = useRef<{ lastX: number; lastY: number; moved: number } | null>(
    null,
  );
  const disengageAutoFit = useCallback(() => {
    if (autoFitRef.current) {
      autoFitRef.current = false;
      setAutoFit(false);
    }
  }, []);
  const engageAutoFit = useCallback(() => {
    autoFitRef.current = true;
    setAutoFit(true);
  }, []);
  // Pointer (canvas CSS px) → sim world coordinates through the camera.
  const toWorld = useCallback((sx: number, sy: number) => {
    const c = cameraRef.current;
    return { x: (sx - c.tx) / c.k, y: (sy - c.ty) / c.k };
  }, []);
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
  // Isolation (Obsidian's local graph): when set, the canvas shows only this
  // node's ego-graph at `isolateHops` depth. Entered via the panel's isolate
  // button or a search hit for an off-cap node; exited via "back to full".
  const [isolatedRef, setIsolatedRef] = useState<string | null>(null);
  const [isolateHops, setIsolateHops] = useState(1);
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

  // Caso 0: re-fit the graph whenever the content panel opens/closes/resizes —
  // the canvas shrinks to the leftover width and the sim reheats to re-centre.
  useEffect(() => {
    refitGraph();
  }, [selected?.id, !!referenceDetail, panelExpanded]); // eslint-disable-line react-hooks/exhaustive-deps

  // Set of node types the user has toggled OFF in the legend. Default hides
  // phantom (unconsolidated noise) and session (scaffolding, not an entity)
  // so a fresh view opens on real, consolidated entities. Clicking a legend
  // chip flips inclusion. Phantom is treated as its own pseudo-type.
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(
    new Set(["phantom", "session"]),
  );

  function toggleType(type: string): void {
    setHiddenTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  // Reheat the simulation whenever the visible set changes (chip toggled,
  // "show all") so the surviving nodes re-distribute across the canvas —
  // the render loop only simulates visible nodes.
  useEffect(() => {
    alphaRef.current = Math.max(alphaRef.current, 0.5);
  }, [hiddenTypes]);

  // Search panel state
  const [search, setSearch] = useState("");
  const [searchResults, setSearchResults] =
    useState<MemorySearchPayload | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);

  // Mobile (Caso 4): no force-graph on a phone (it's near-useless on touch).
  // Below this width the graph option is hidden and cards take its place;
  // panels go full-screen — one surface at a time.
  const [compact, setCompact] = useState(
    () => typeof window !== "undefined" && window.innerWidth < 720,
  );
  useEffect(() => {
    const onResize = () => setCompact(window.innerWidth < 720);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  const effectiveView = compact && view === "graph" ? "cards" : view;

  // Caso 2: the graph recedes when the search panel is up with a live query.
  // Graph view only — in cards/table the search filters the grid directly.
  const searching =
    effectiveView === "graph" &&
    searchOpen &&
    (search.trim().length > 0 || searchResults != null);
  recedeRef.current = searching;

  // Edge popup state
  const [edgePopup, setEdgePopup] = useState<{
    x: number; y: number; detail: MemoryEdgeDetail | null; loading: boolean;
  } | null>(null);

  // Two-layer graph (overview bubbles/hubs/loose → drill into a cluster or
  // an ego neighbourhood). The hook owns the layer state machine; this view
  // only has to plug its result into the existing data seam and drill
  // functions below, kept under their pre-existing names so every call site
  // (depth buttons, the panel's focus button, handleOpenEntity, search hits)
  // keeps working unchanged.
  const layers = useGraphLayers(
    _props.active && effectiveView === "graph",
    () => tokenRef.current,
  );
  // The overview only replaces the canvas payload in clustered mode; a flat
  // workspace (too small to bubble) has nothing to translate, so the canvas
  // falls back to today's capped graph instead.
  const overviewGraph = useMemo(
    () =>
      layers.overview && layers.overview.mode === "clustered"
        ? overviewToGraph(layers.overview)
        : null,
    [layers.overview],
  );
  // THE SEAM: graph view only — a cluster/ego drill wins, then the clustered
  // overview, then today's raw graph. Cards/table never look at the overview
  // OR a drill: they present the full entity list rather than a navigable
  // bubble map, so a drill entered from the canvas must not truncate their
  // grid down to the focused subgraph.
  const data =
    effectiveView === "graph" ? layers.focusGraph ?? overviewGraph ?? rawData : rawData;

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

  // Switch the canvas to a node's ego-graph (uncapped neighbourhood) at the
  // given depth — a thin wrapper over the shared layer state machine so
  // isolating a node also updates the breadcrumb. Entered from the panel's
  // isolate button, the depth control, and search hits for nodes the global
  // cap dropped; "back to full" / the breadcrumb / Esc all exit it.
  const isolateNode = useCallback(
    (ref: string, hops: number) => {
      setIsolateHops(hops);
      const name =
        data?.nodes.find((n) => n.id === ref)?.name ??
        ref.replace(/^[a-z_]+:/, "");
      void layers.enterEgo(ref, name, hops);
    },
    [data, layers],
  );

  const exitIsolation = useCallback(() => {
    layers.backToOverview();
  }, [layers]);

  // isolatedRef mirrors the hook's layer so the canvas can keep ring-
  // highlighting the isolated node; it's only meaningful for an ego drill.
  useEffect(() => {
    setIsolatedRef(layers.layer.kind === "ego" ? layers.layer.ref : null);
  }, [layers.layer]);

  // Esc backs out one layer (cluster/ego → overview) — but not while the
  // search panel or the edge popover is up, so Escape closes those first.
  // Listens on `window` (rather than `document`): in the bubble phase,
  // `document`-level listeners fire before `window`-level ones, so a
  // popover that listens on `document` — e.g. MemoryTypeFilter's own Escape
  // handler — always gets first refusal on the same keypress. It calls
  // `preventDefault()` when it actually closes; checking that here means
  // one Escape closes only the top-most layer instead of cascading through
  // both at once.
  useEffect(() => {
    if (layers.layer.kind === "overview") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (e.defaultPrevented) return;
      if (searchOpen || edgePopup) return;
      layers.backToOverview();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [layers, searchOpen, edgePopup]);

  // Local dismissal for the stale-cluster pill (contract c). The hook nulls
  // `notice` at the start of every enterCluster/enterEgo call before ever
  // re-setting it, so a fresh staleness event is always a null→"staleCluster"
  // transition — re-arming here on that transition means a dismissed pill
  // comes back for a *new* stale hit instead of staying hidden forever.
  const [staleDismissed, setStaleDismissed] = useState(false);
  useEffect(() => {
    if (layers.notice === "staleCluster") setStaleDismissed(false);
  }, [layers.notice]);

  // Map-changed toast (contract d): re-check the overview when the tab
  // regains focus, but only while sitting at the top layer — drilled into a
  // cluster/ego, the visible canvas isn't the overview anyway, and
  // re-laying-out bubbles under the user mid-interaction is exactly what
  // this must not do. Passive: refreshOverview() already applies the new
  // payload; the toast only announces that it happened.
  const [mapChanged, setMapChanged] = useState(false);
  useEffect(() => {
    if (!_props.active || effectiveView !== "graph") return;
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (layers.layer.kind !== "overview") return;
      void layers.refreshOverview().then((changed) => {
        if (changed) setMapChanged(true);
      });
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [_props.active, effectiveView, layers]);

  useEffect(() => {
    if (!mapChanged) return;
    const id = setTimeout(() => setMapChanged(false), 4000);
    return () => clearTimeout(id);
  }, [mapChanged]);

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

  // Canvas chrome (edges, rings, label text) tracks durin's active colour
  // tokens instead of hardcoded literals, so a palette or light/dark change
  // repaints the graph too. Resolved once up front and re-resolved only on
  // a theme change (watchTheme), not per frame — resolving a CSS custom
  // property is a synchronous style read, too costly to repeat 60x/sec.
  const themeRef = useRef<CanvasTheme | null>(null);
  if (themeRef.current === null) themeRef.current = readCanvasTheme();
  useEffect(() => watchTheme(() => { themeRef.current = readCanvasTheme(); }), []);

  // RAF render loop — graph view only (the canvas is unmounted otherwise).
  useEffect(() => {
    if (!_props.active || effectiveView !== "graph") return;
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let stopped = false;
    // Read once per mount rather than inside frame(), which runs every
    // animation frame (see prefersReducedMotion).
    const ease = prefersReducedMotion() ? 1 : 0.08;

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

    // Manual zoom (Obsidian: scroll wheel), anchored on the cursor. Native
    // non-passive listener — a passive one cannot preventDefault page scroll.
    function onWheel(e: WheelEvent) {
      e.preventDefault();
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const sx = e.clientX - rect.left;
      const sy = e.clientY - rect.top;
      const c = cameraRef.current;
      const k2 = clamp(c.k * Math.exp(-e.deltaY * 0.0015), 0.25, 4);
      c.tx = sx - ((sx - c.tx) / c.k) * k2;
      c.ty = sy - ((sy - c.ty) / c.k) * k2;
      c.k = k2;
      disengageAutoFit();
    }
    canvas.addEventListener("wheel", onWheel, { passive: false });

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
        // Simulate only the nodes the type filters left visible — hidden
        // nodes must not keep repelling the survivors, or the layout stays
        // spread out as if they were still there instead of re-flowing to
        // fill the canvas. Their positions freeze until re-shown (a filter
        // change reheats the sim, so they re-integrate on return).
        const activeNodes = nodes.filter(isVisible);
        const activeEdges = edges.filter(
          (e) => isVisible(e.source) && isVisible(e.target),
        );
        tickForces(activeNodes, activeEdges, w, h, alpha);
        alphaRef.current = alpha * 0.985;
      }

      // Auto-fit: ease the camera toward framing the visible nodes' bounding
      // box. Zoom-in is clamped so a lone filtered node stays node-sized
      // instead of ballooning; zoom-out below 1 only happens for the margins
      // (the sim walls keep content inside the canvas).
      if (autoFitRef.current) {
        let minX = Infinity;
        let maxX = -Infinity;
        let minY = Infinity;
        let maxY = -Infinity;
        for (const n of nodes) {
          if (!isVisible(n)) continue;
          const r = nodeRadius(n) + 18;
          if (n.x - r < minX) minX = n.x - r;
          if (n.x + r > maxX) maxX = n.x + r;
          if (n.y - r < minY) minY = n.y - r;
          if (n.y + r > maxY) maxY = n.y + r;
        }
        if (minX !== Infinity) {
          const pad = 36;
          const bw = Math.max(1, maxX - minX);
          const bh = Math.max(1, maxY - minY);
          const tk = Math.min(1.6, (w - pad * 2) / bw, (h - pad * 2) / bh);
          const ttx = w / 2 - ((minX + maxX) / 2) * tk;
          const tty = h / 2 - ((minY + maxY) / 2) * tk;
          const cam = cameraRef.current;
          cam.k += (tk - cam.k) * ease;
          cam.tx += (ttx - cam.tx) * ease;
          cam.ty += (tty - cam.ty) * ease;
        }
      }

      const dpr = window.devicePixelRatio || 1;
      const cam = cameraRef.current;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.setTransform(dpr * cam.k, 0, 0, dpr * cam.k, dpr * cam.tx, dpr * cam.ty);

      // Caso 2: recede the whole graph behind the search results — but only
      // while there are no matched nodes to highlight. Once searchMatchSet
      // exists, the per-node dimming (matches lit, rest faint) IS the signal;
      // a uniform veil on top would grey out the very nodes the search found.
      // Named (not just left in ctx.globalAlpha) because every themed element
      // below restores to it, not to 1, after its own temporary alpha
      // override — the veil must keep applying to whatever draws next.
      const veil = recedeRef.current && !searchMatchSet ? 0.18 : 1;
      ctx.globalAlpha = veil;

      // Hover highlight (Obsidian's graph hover): while the pointer rests on
      // a node, that node + its direct connections light up and the rest
      // fades. Transient by construction — computed per frame from hoverRef,
      // no React state, gone the moment the pointer leaves.
      const hovered = hoverRef.current;
      let hoverSet: Set<string> | null = null;
      if (hovered) {
        hoverSet = new Set([hovered.id]);
        for (const e of edges) {
          if (e.source.id === hovered.id) hoverSet.add(e.target.id);
          else if (e.target.id === hovered.id) hoverSet.add(e.source.id);
        }
      }
      // A node is highlighted iff it passes BOTH active dimming layers
      // (hover AND search compose multiplicatively).
      const isHighlighted = (id: string): boolean => {
        if (hoverSet && !hoverSet.has(id)) return false;
        if (searchMatchSet && !searchMatchSet.has(id)) return false;
        return true;
      };

      ctx.lineCap = "round";
      for (const e of edges) {
        // Hidden endpoints → don't draw the edge at all.
        if (!isVisible(e.source) || !isVisible(e.target)) continue;
        const lit = isHighlighted(e.source.id) && isHighlighted(e.target.id);
        // themeRef.current.line resolves to an opaque colour, so the lit/dim
        // distinction (previously baked into the rgba alpha channel) now
        // goes through globalAlpha instead.
        ctx.globalAlpha = veil * (lit ? Math.min(0.55, 0.18 + e.weight * 0.06) : 0.08);
        ctx.strokeStyle = themeRef.current!.line;
        ctx.lineWidth = Math.min(3, 0.8 + Math.log(1 + e.weight));
        ctx.beginPath();
        ctx.moveTo(e.source.x, e.source.y);
        ctx.lineTo(e.target.x, e.target.y);
        ctx.stroke();
      }
      ctx.globalAlpha = veil;

      for (const n of nodes) {
        if (!isVisible(n)) continue;
        const r = nodeRadius(n);
        const lit = isHighlighted(n.id);
        if (n.kind === "bubble") {
          // A bubble stands in for a whole cluster — drawn hollow so it
          // reads as a container around member entities, not as one more
          // entity itself.
          ctx.beginPath();
          ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
          ctx.fillStyle = themeRef.current!.surface;
          ctx.fill();
          ctx.strokeStyle = lit ? themeRef.current!.accent : themeRef.current!.border;
          ctx.lineWidth = 1.4;
          ctx.stroke();
        } else if (n.type === "session") {
          ctx.beginPath();
          ctx.rect(n.x - r, n.y - r, r * 2, r * 2);
          ctx.fillStyle = lit ? colorForType("session") : `${colorForType("session")}33`;
          ctx.fill();
        } else {
          const fill = colorForType(n.type);
          ctx.beginPath();
          ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
          ctx.fillStyle = lit ? fill : `${fill}33`;
          ctx.fill();
        }
        if (n.phantom) {
          ctx.setLineDash([3, 3]);
          ctx.globalAlpha = veil * (lit ? 0.4 : 0.15);
          ctx.strokeStyle = themeRef.current!.textMuted;
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.globalAlpha = veil;
        }
        if (
          selected?.id === n.id ||
          hoverRef.current?.id === n.id ||
          isolatedRef === n.id
        ) {
          const isolating = isolatedRef === n.id;
          ctx.beginPath();
          ctx.arc(n.x, n.y, r + 3, 0, Math.PI * 2);
          ctx.globalAlpha = veil * (isolating ? 0.75 : 0.55);
          ctx.strokeStyle = isolating ? themeRef.current!.accent : themeRef.current!.text;
          ctx.lineWidth = 1.6;
          ctx.stroke();
          ctx.globalAlpha = veil;
        }
      }

      // Label pass runs in screen space with a constant font size — resetting
      // the transform here (instead of keeping the camera's world-space
      // transform the edge/node passes drew under) means label text never
      // scales with zoom the way node/edge geometry does.
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.font = "11px ui-sans-serif, system-ui, -apple-system";
      ctx.textBaseline = "top";
      ctx.textAlign = "center";
      // A label anchors below its node's circle, except bubbles: their
      // name + count line render centered inside the hollow circle instead.
      const labelSy = (n: SimNode, r: number): number =>
        n.kind === "bubble"
          ? n.y * cam.k + cam.ty - 8
          : (n.y + r) * cam.k + cam.ty + 2;
      const cands: LabelCandidate[] = [];
      for (const n of nodes) {
        if (!isVisible(n)) continue;
        const r = nodeRadius(n);
        cands.push({
          id: n.id,
          sx: n.x * cam.k + cam.tx,
          sy: labelSy(n, r),
          weight: n.weight,
          priority:
            n.kind === "bubble" ||
            hoverSet?.has(n.id) === true ||
            selected?.id === n.id ||
            isolatedRef === n.id,
        });
      }
      const show = visibleLabels(cands, { w, h }, labelBudget(cam.k));
      for (const n of nodes) {
        if (!show.has(n.id)) continue;
        const r = nodeRadius(n);
        const sx = n.x * cam.k + cam.tx;
        const sy = labelSy(n, r);
        const lit = isHighlighted(n.id);
        if (n.kind === "bubble") {
          const sr = r * cam.k;
          const displayName =
            n.name === "__others__" ? t("memoryGraph.clusterOthers") : n.name;
          ctx.font = "500 13px ui-sans-serif, system-ui, -apple-system";
          ctx.fillStyle = themeRef.current!.text;
          ctx.fillText(shortLabel(displayName), sx, sy);
          ctx.font = "11px ui-sans-serif, system-ui, -apple-system";
          if (sr >= 28) {
            ctx.fillStyle = themeRef.current!.textMuted;
            ctx.fillText(
              t("memoryGraph.bubbleEntities", { count: n.count ?? 0 }),
              sx,
              sy + 15,
            );
          }
        } else {
          ctx.fillStyle = lit ? themeRef.current!.text : themeRef.current!.textMuted;
          ctx.fillText(shortLabel(n.name), sx, sy);
        }
      }

      ctx.globalAlpha = 1;
      rafRef.current = requestAnimationFrame(frame);
    }
    rafRef.current = requestAnimationFrame(frame);

    return () => {
      stopped = true;
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      ro.disconnect();
      canvas.removeEventListener("wheel", onWheel);
    };
  }, [_props.active, effectiveView, selected, isolatedRef, searchMatchSet, hiddenTypes, compact, disengageAutoFit]);

  // Hit-test (for nodes AND edges). Skips nodes hidden by legend
  // toggles so the user can't accidentally select a node that's not
  // even rendered.
  const hitTestNode = useCallback((x: number, y: number): SimNode | null => {
    const nodes = simNodesRef.current;
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      if (hiddenTypes.has(n.type)) continue;
      if (n.phantom && hiddenTypes.has("phantom")) continue;
      const r = nodeRadius(n) + 4;
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
      const sx = evt.clientX - rect.left;
      const sy = evt.clientY - rect.top;
      const { x, y } = toWorld(sx, sy);
      const hit = hitTestNode(x, y);
      // Overview mode: a press on a node starts a drag exactly like the
      // normal path below (pin, draggingRef, pointer capture) — it must NOT
      // navigate immediately, or the node could never be repositioned.
      // Whether this turns out to be a click (drill into the bubble's
      // cluster, or straight into an ego neighbourhood for a hub/loose node)
      // or a real drag is decided on release, by comparing the pointer's
      // screen position then vs. now (see onPointerUp). Once a cluster/ego
      // is entered the canvas shows a real (non-overview) graph and clicks
      // fall through to the existing select behavior below.
      if (
        hit &&
        layers.layer.kind === "overview" &&
        overviewGraph != null &&
        layers.focusGraph == null
      ) {
        hit.pinned = true;
        hit.vx = 0;
        hit.vy = 0;
        draggingRef.current = hit;
        overviewPressRef.current = { id: hit.id, x: sx, y: sy };
        evt.currentTarget.setPointerCapture(evt.pointerId);
        return;
      }
      if (hit) {
        hit.pinned = true;
        hit.vx = 0;
        hit.vy = 0;
        draggingRef.current = hit;
        setSelected(hit);
        // Click = open the content panel, nothing more (Obsidian's contract:
        // hover highlights, click navigates). The graph stays as-is; isolation
        // is an explicit action via the panel's isolate button.
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
        // Open edge popup near the midpoint. The popup is a DOM overlay in
        // screen space, so project the sim-space midpoint through the camera
        // at click time.
        const cam = cameraRef.current;
        const mx = ((edgeHit.source.x + edgeHit.target.x) / 2) * cam.k + cam.tx;
        const my = ((edgeHit.source.y + edgeHit.target.y) / 2) * cam.k + cam.ty;
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
      // Background press: start a pan (Obsidian: drag to move the view). If
      // the pointer barely moves it's a click — selection clears on release.
      panRef.current = { lastX: sx, lastY: sy, moved: 0 };
      evt.currentTarget.setPointerCapture(evt.pointerId);
    },
    [hitTestNode, hitTestEdge, toWorld, layers, overviewGraph],
  );

  const onPointerMove = useCallback(
    (evt: React.PointerEvent<HTMLCanvasElement>) => {
      const rect = evt.currentTarget.getBoundingClientRect();
      const sx = evt.clientX - rect.left;
      const sy = evt.clientY - rect.top;
      const pan = panRef.current;
      if (pan) {
        const dx = sx - pan.lastX;
        const dy = sy - pan.lastY;
        pan.moved += Math.abs(dx) + Math.abs(dy);
        pan.lastX = sx;
        pan.lastY = sy;
        if (pan.moved > 4) {
          const c = cameraRef.current;
          c.tx += dx;
          c.ty += dy;
          disengageAutoFit();
        }
        return;
      }
      const { x, y } = toWorld(sx, sy);
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
            // Preview popover is positioned in screen space, not sim space.
            const px = sx;
            const py = sy;
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
                  const body = stripHtmlComments(d?.page?.body ?? "")
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
    [hitTestNode, hitTestEdge, toWorld, disengageAutoFit],
  );

  const onPointerUp = useCallback(
    (evt: React.PointerEvent<HTMLCanvasElement>) => {
      const pan = panRef.current;
      if (pan) {
        panRef.current = null;
        evt.currentTarget.releasePointerCapture(evt.pointerId);
        // A press that never really moved is a background click: clear the
        // selection/popup (the pre-pan behavior of empty-space clicks).
        if (pan.moved <= 4) {
          setSelected(null);
          setEdgePopup(null);
        }
        return;
      }
      const drag = draggingRef.current;
      const press = overviewPressRef.current;
      if (drag) {
        drag.pinned = false;
        draggingRef.current = null;
        alphaRef.current = Math.max(alphaRef.current, 0.3);
        evt.currentTarget.releasePointerCapture(evt.pointerId);
      }
      if (press) {
        overviewPressRef.current = null;
        // Click-vs-drag: only route (drill in) when the released node is
        // the one we pressed AND the pointer barely moved. Movement past
        // the threshold is a real drag — the node keeps its new position
        // (already applied live in onPointerMove) and nothing navigates.
        const rect = evt.currentTarget.getBoundingClientRect();
        const sx = evt.clientX - rect.left;
        const sy = evt.clientY - rect.top;
        const moved = Math.hypot(sx - press.x, sy - press.y);
        if (drag && drag.id === press.id && moved < 5) {
          if (drag.kind === "bubble") {
            void layers.enterCluster(drag.id, drag.name);
          } else {
            void layers.enterEgo(drag.id, drag.name);
          }
        }
      }
    },
    [layers],
  );

  // Fetch detail whenever the selection changes — branch by type.
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setSessionDetail(null);
      setDetailError(null);
      return;
    }
    // Reference nodes aren't entities — they have no entity-detail endpoint.
    // Hand off to the reference panel (same path as a reference search hit):
    // clear the selection and load the document into `referenceDetail`.
    if (selected.type === "reference") {
      const refId = selected.id;
      setSelected(null);
      if (tokenRef.current) {
        void fetchMemoryEntry(tokenRef.current, refId)
          .then((d) => setReferenceDetail(d))
          .catch(() => setReferenceDetail(null));
      }
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

  // Run search whenever the query stabilises. Graph view only: the semantic
  // search API powers the results panel + node highlighting there. Cards and
  // table filter their grid live from the payload instead (name / alias /
  // summary substring), so no backend round-trip.
  useEffect(() => {
    const q = search.trim();
    if (!q || effectiveView !== "graph") {
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
  }, [search, effectiveView]);

  const typesLegend = useMemo(() => {
    if (!data) return [] as { type: string; color: string; count: number }[];
    const counts = new Map<string, number>();
    for (const n of data.nodes) counts.set(n.type, (counts.get(n.type) ?? 0) + 1);
    return data.stats.types.map((t) => ({
      type: t,
      color: colorForType(t),
      count: counts.get(t) ?? 0,
    }));
  }, [data]);

  // Hide every type at once (the "start from nothing, reveal one" flow the
  // subtractive chip row couldn't express); phantom is a pseudo-type toggle.
  const hideAllTypes = useCallback(() => {
    if (!data) return;
    const next = new Set<string>(data.stats.types);
    if (data.stats.phantom_count > 0) next.add("phantom");
    setHiddenTypes(next);
  }, [data]);

  // Solo: show only `type` (hide all others, and phantom unless it's the solo).
  const soloType = useCallback(
    (type: string) => {
      if (!data) return;
      const next = new Set<string>(data.stats.types.filter((t) => t !== type));
      if (data.stats.phantom_count > 0 && type !== "phantom") next.add("phantom");
      setHiddenTypes(next);
    },
    [data],
  );

  // The clustered overview has nothing to filter by type: the server
  // already excluded phantoms/sessions before bubbling, and a bubble isn't
  // itself a type. The filter comes back once there's a real per-type node
  // set to browse — a cluster/ego drill, flat mode, or the cards/table
  // presentations (which always read the full entity list).
  const showTypeFilter =
    effectiveView !== "graph" || layers.focusGraph != null || overviewGraph == null;

  // Real (non-scaffolding) node count in the current drill — phantom and
  // session nodes are kept in a cluster's focusGraph only as scaffolding
  // around the real members, so they're excluded before comparing against
  // the server's uncapped totalMembers. Computed once and shared by both
  // the breadcrumb's "and N more" gate and the number it prints, so the two
  // can't drift apart.
  const realShown =
    data?.nodes.filter((n) => !n.phantom && n.type !== "session").length ?? 0;

  // First-load skeleton (contract c2): nothing has ever resolved yet for
  // either the raw graph or the overview. `!error` defers to the pre-existing
  // raw-error overlay below instead of papering over it with a placeholder.
  const showFirstLoadSkeleton =
    effectiveView === "graph" &&
    !error &&
    layers.loading &&
    layers.overview == null &&
    rawData == null;

  // Teaching empty state (contract a): the raw list AND the overview both
  // confirm zero entities — a genuinely empty workspace, not just an
  // unloaded or drilled-empty view. `focusGraph == null` keeps this from
  // ever covering an active cluster/ego drill still showing its own (real)
  // content. Supersedes the generic "no entity pages" overlay below for
  // this specific case (see its own guard).
  const showTeachingEmpty =
    effectiveView === "graph" &&
    !error &&
    !loading &&
    layers.focusGraph == null &&
    rawData != null &&
    rawData.nodes.length === 0 &&
    (layers.overview?.stats.entity_count ?? 0) === 0;

  // Select an entity from the cards grid or the table — same panel wiring as
  // a graph-canvas click, minus the canvas-only concerns (pinning, drag).
  const selectEntity = useCallback((n: MemoryGraphNode) => {
    setSelected(n);
    setPanelExpanded(false);
    setActiveTab(n.phantom ? "info" : "body");
    setReferenceDetail(null);
    setEdgePopup(null);
  }, []);

  // From the Documents shelf back into the graph: open a doc-derived entity's
  // page (isolate it if the global cap dropped it, then select).
  const handleOpenEntity = useCallback(
    (ref: string) => {
      setMode("entities");
      setReferenceDetail(null);
      if (!simNodesRef.current.some((n) => n.id === ref)) isolateNode(ref, 1);
      const node =
        simNodesRef.current.find((n) => n.id === ref) ?? {
          id: ref,
          type: ref.split(":")[0] || "unknown",
          name: ref.replace(/^[a-z_]+:/, ""),
          weight: 0,
          aliases: [],
          phantom: false,
        };
      setSelected(node);
      setActiveTab(node.phantom ? "info" : "body");
    },
    [isolateNode],
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2">
        <Network className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-sm font-semibold">{t("memoryGraph.title")}</h1>
        <div className="ml-3 flex items-center gap-0.5 rounded-md border border-border/50 p-0.5 text-[11px]">
          <button
            type="button"
            onClick={() => setMode("entities")}
            className={cn(
              "flex items-center gap-1 rounded px-2 py-0.5 transition-colors",
              mode === "entities"
                ? "bg-muted font-medium"
                : "text-muted-foreground hover:bg-muted/60",
            )}
          >
            <Network className="h-3 w-3" /> {t("memoryGraph.tabEntities")}
          </button>
          <button
            type="button"
            onClick={() => setMode("documents")}
            className={cn(
              "flex items-center gap-1 rounded px-2 py-0.5 transition-colors",
              mode === "documents"
                ? "bg-muted font-medium"
                : "text-muted-foreground hover:bg-muted/60",
            )}
          >
            <BookOpen className="h-3 w-3" /> {t("memoryGraph.viewDocuments")}
          </button>
        </div>
        {mode === "entities" ? (
          <>
        {data && !compact ? (
          <span className="text-xs text-muted-foreground">
            {effectiveView === "graph" && overviewGraph != null && layers.focusGraph == null && layers.overview ? (
              // Clustered overview, no drill, graph view: report the
              // workspace's honest totals (from the overview's own stats)
              // instead of the canvas's capped bubble/hub/loose node count.
              // Gated on graph view too, or Cards/Table (which always show
              // the full rawData list) would inherit this graph-only count.
              <>
                {t("memoryGraph.entitiesTotal", { count: layers.overview.stats.entity_count })}
                {" · "}
                {t("memoryGraph.groupsCount", { count: layers.overview.stats.bubble_count })}
              </>
            ) : (
              t("memoryGraph.stats", {
                nodesLabel: t("memoryGraph.nodesCount", { count: data.stats.node_count }),
                edgesLabel: t("memoryGraph.edgesCount", { count: data.stats.edge_count }),
              })
            )}
            {data.stats.phantom_count > 0
              ? ` · ${t("memoryGraph.statsPhantom", { count: data.stats.phantom_count })}`
              : ""}
            {data.stats.truncated_nodes || data.stats.truncated_edges
              ? ` · ${t("memoryGraph.statsTruncated")}`
              : ""}
          </span>
        ) : null}
        {layers.focusGraph && effectiveView === "graph" ? (
          <>
            <button
              type="button"
              onClick={exitIsolation}
              className="rounded border border-border/40 px-2 py-0.5 text-[11px] text-primary hover:bg-muted"
            >
              ← {t("memoryGraph.backToFull")}
            </button>
            {isolatedRef ? (
              // Depth of the isolated neighbourhood (Obsidian's local-graph
              // depth control): each level adds the nodes connected to the
              // previous level. Server clamps to 1–3.
              <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
                {t("memoryGraph.isolateDepth")}
                <span className="flex items-center gap-0.5 rounded-md border border-border/50 p-0.5">
                  {[1, 2, 3].map((h) => (
                    <button
                      key={h}
                      type="button"
                      onClick={() => isolateNode(isolatedRef, h)}
                      className={cn(
                        "rounded px-1.5 py-0.5 transition-colors",
                        isolateHops === h
                          ? "bg-primary/10 font-medium text-primary"
                          : "hover:bg-muted",
                      )}
                    >
                      {h}
                    </button>
                  ))}
                </span>
              </span>
            ) : null}
          </>
        ) : null}
        <div className="ml-auto flex min-w-0 items-center gap-2">
          <div className={cn("relative", compact && "min-w-0 flex-1")}>
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
                "h-7 rounded-md border border-input bg-background pl-7 pr-2 text-[12.5px]",
                compact ? "w-full" : "w-72",
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
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("memoryGraph.refresh")}
            onClick={() => {
              void refresh();
              // Graph view is driven by the two-layer overview/focus data,
              // not the raw list — refresh that too, or the canvas would
              // sit stale while only the (invisible) raw list updated.
              if (effectiveView === "graph") void layers.refreshOverview();
            }}
            disabled={loading}
            className="h-7 w-7"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          </Button>
        </div>
          </>
        ) : null}
      </header>

      {/* Entities toolbar — the view switcher (three presentations of the
          same node set, Obsidian-Bases style), the type filter chips (shared
          by all three views; previously a graph-only floating legend), and
          the sort control for the cards/table grids. */}
      {mode === "entities" ? (
        <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-border/40 px-3 py-1.5 text-[11px]">
          <div className="flex items-center gap-0.5 rounded-md border border-border/50 p-0.5">
            {!compact ? (
              <button
                type="button"
                onClick={() => setViewPersisted("graph")}
                className={cn(
                  "flex items-center gap-1 rounded px-2 py-0.5 transition-colors",
                  effectiveView === "graph"
                    ? "bg-primary/10 font-medium text-primary"
                    : "text-muted-foreground hover:bg-muted/60",
                )}
              >
                <Network className="h-3 w-3" /> {t("memoryGraph.viewGraph")}
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => setViewPersisted("cards")}
              className={cn(
                "flex items-center gap-1 rounded px-2 py-0.5 transition-colors",
                effectiveView === "cards"
                  ? "bg-primary/10 font-medium text-primary"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
            >
              <LayoutGrid className="h-3 w-3" /> {t("memoryGraph.viewCards")}
            </button>
            <button
              type="button"
              onClick={() => setViewPersisted("table")}
              className={cn(
                "flex items-center gap-1 rounded px-2 py-0.5 transition-colors",
                effectiveView === "table"
                  ? "bg-primary/10 font-medium text-primary"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
            >
              <Table2 className="h-3 w-3" /> {t("memoryGraph.viewTable")}
            </button>
          </div>
          {showTypeFilter &&
          (typesLegend.length > 0 || (data?.stats.phantom_count ?? 0) > 0) ? (
            <span className="mx-0.5 h-4 w-px bg-border/60" aria-hidden />
          ) : null}
          {showTypeFilter ? (
            <MemoryTypeFilter
              types={typesLegend}
              phantomCount={data?.stats.phantom_count ?? 0}
              hidden={hiddenTypes}
              onToggle={toggleType}
              onShowAll={() => setHiddenTypes(new Set())}
              onHideAll={hideAllTypes}
              onSolo={soloType}
            />
          ) : null}
          {effectiveView === "cards" ? (
            <label className="ml-auto flex items-center gap-1 text-muted-foreground">
              <ArrowDownUp className="h-3 w-3" aria-hidden />
              <select
                value={sortKey}
                onChange={(e) => setSortKey(e.target.value as EntitySortKey)}
                className="h-6 rounded border border-input bg-background px-1 text-[11px] outline-none focus:ring-1 focus:ring-ring"
              >
                <option value="recent">{t("memoryGraph.sortRecent")}</option>
                <option value="mentions">{t("memoryGraph.sortMentions")}</option>
                <option value="name">{t("memoryGraph.sortName")}</option>
              </select>
            </label>
          ) : null}
        </div>
      ) : null}

      {mode === "documents" ? (
        <DocumentsShelf
          token={token}
          active={_props.active}
          onOpenEntity={handleOpenEntity}
        />
      ) : (
      <>
        {/* Breadcrumb: shown only while drilled into a cluster/ego layer —
            the clustered overview IS the top level, so it gets no crumb of
            its own. A normal flow row (not an overlay) so the canvas below
            re-fits into the remaining height via wrapRef's flex-1, the same
            way the toolbar row above it already does. */}
        {effectiveView === "graph" && layers.layer.kind !== "overview" ? (
          <div className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-1.5 text-sm">
            <button
              type="button"
              onClick={() => layers.backToOverview()}
              className="text-primary hover:underline"
            >
              {t("memoryGraph.title")}
            </button>
            <span className="text-muted-foreground">›</span>
            <span className="font-medium">{layers.layer.name}</span>
            {layers.layer.kind === "cluster" &&
            layers.totalMembers != null &&
            realShown < layers.totalMembers ? (
              // realShown already excludes phantom/session scaffolding nodes
              // (see its definition above) — reusing it here for the number
              // keeps the gate and the printed count from drifting apart.
              <span className="ml-auto text-xs text-muted-foreground">
                {t("memoryGraph.andMore", { count: layers.totalMembers - realShown })}
              </span>
            ) : null}
          </div>
        ) : null}
        {/* Stale-cluster notice: a drill 404'd (the cluster changed shape
            since the overview was built) — the hook already fell back to
            overview and kicked off a background refresh; this just tells the
            user why they landed back here. Locally dismissible; re-armed by
            the effect above whenever a fresh staleness event lands. */}
        {effectiveView === "graph" && layers.notice === "staleCluster" && !staleDismissed ? (
          <div
            role="status"
            aria-live="polite"
            className="flex shrink-0 items-center gap-2 border-b border-border/40 bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground"
          >
            <span className="flex-1">{t("memoryGraph.staleCluster")}</span>
            <button
              type="button"
              aria-label={t("memoryGraph.close")}
              onClick={() => setStaleDismissed(true)}
              className="rounded p-0.5 hover:bg-muted"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        ) : null}
        {/* Overview call failed but there's still something on screen — either
            the last good overview payload, or (when the overview has never
            succeeded) the raw graph loaded independently via useMemoryGraph.
            Either way, keep the canvas showing it (below) and just flag the
            failure instead of blanking a working view. */}
        {effectiveView === "graph" &&
        layers.error != null &&
        (layers.overview != null || rawData != null) ? (
          <div
            role="alert"
            className="flex shrink-0 items-center gap-2 border-b border-border/40 bg-destructive/10 px-3 py-1.5 text-xs text-destructive"
          >
            <span className="flex-1">{t("memoryGraph.loadError")}</span>
            <Button
              variant="outline"
              size="sm"
              className="h-6 px-2 text-[11px]"
              onClick={() => void layers.refreshOverview()}
            >
              {t("memoryGraph.retry")}
            </Button>
          </div>
        ) : null}
      <div ref={wrapRef} className="relative min-h-0 flex-1">
        {error ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-sm text-destructive">
            <span>{error}</span>
            <Button variant="outline" size="sm" onClick={() => void refresh()}>
              {t("memoryGraph.retry")}
            </Button>
          </div>
        ) : null}
        {/* Nothing has ever loaded on either endpoint and the overview call
            is the one failing — the slim banner above can't show (it needs
            a last-good overview), so fail the whole canvas area instead of
            leaving it blank. */}
        {effectiveView === "graph" && !error && layers.error != null && layers.overview == null && rawData == null ? (
          <div
            role="alert"
            className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-sm text-destructive"
          >
            <span>{t("memoryGraph.loadError")}</span>
            <Button variant="outline" size="sm" onClick={() => void layers.refreshOverview()}>
              {t("memoryGraph.retry")}
            </Button>
          </div>
        ) : null}
        {showFirstLoadSkeleton ? (
          <div className="absolute inset-0 p-4">
            <div className="h-full w-full animate-pulse rounded-lg bg-muted/60" />
          </div>
        ) : null}
        {!showFirstLoadSkeleton && loading && !data ? (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
            {t("memoryGraph.loadingGraph")}
          </div>
        ) : null}
        {showTeachingEmpty ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 px-8 text-center">
            <Network className="h-10 w-10 text-muted-foreground/40" aria-hidden />
            <p className="text-sm font-medium text-foreground">{t("memoryGraph.emptyTitle")}</p>
            <p className="max-w-sm text-xs text-muted-foreground">{t("memoryGraph.emptyBody")}</p>
          </div>
        ) : null}
        {!showTeachingEmpty && data && data.nodes.length === 0 ? (
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
        {/* Drill load: a depth change or a new cluster/ego fetch is in
            flight while the canvas already has something to show — keep
            rendering it and just flag the fetch with a thin bar instead of
            blanking or skeleton-ing over a working view. */}
        {effectiveView === "graph" && layers.loading && layers.overview != null ? (
          <div className="absolute inset-x-0 top-0 z-10 h-0.5 overflow-hidden bg-primary/15">
            <div className="h-full w-full animate-pulse bg-primary/60" />
          </div>
        ) : null}
        {effectiveView === "graph" ? (
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
        ) : null}
        {/* Re-engage auto-framing after a manual zoom/pan (Obsidian leaves
            the camera fully manual; we default back to auto on request). */}
        {effectiveView === "graph" && !autoFit ? (
          <Button
            size="sm"
            variant="outline"
            onClick={engageAutoFit}
            className="absolute bottom-3 left-3 z-10 h-7 gap-1 bg-background/85 text-[11px] backdrop-blur"
          >
            <Scan className="h-3 w-3" /> {t("memoryGraph.fitView")}
          </Button>
        ) : null}
        {/* Passive map-changed toast (contract d) — announces a background
            overview refresh triggered by the tab regaining focus; the new
            payload is already applied by the time this shows. */}
        {effectiveView === "graph" && mapChanged ? (
          <div
            role="status"
            aria-live="polite"
            className="absolute right-3 top-3 z-10 rounded-md border border-border/50 bg-card/95 px-3 py-1.5 text-[11px] shadow-lg backdrop-blur"
          >
            {t("memoryGraph.mapChanged")}
          </div>
        ) : null}
        {effectiveView !== "graph" ? (
          // Cards / table presentations. When the desktop compact detail
          // panel is open it reserves the same right-hand column the graph
          // canvas gives up, so the grid re-flows beside it instead of
          // hiding rows underneath.
          <div
            className="absolute inset-0"
            style={
              selected && !compact && !panelExpanded
                ? { paddingRight: "23rem" }
                : undefined
            }
          >
            {effectiveView === "cards" ? (
              <MemoryEntityCards
                nodes={data?.nodes ?? []}
                hiddenTypes={hiddenTypes}
                query={search}
                sortKey={sortKey}
                onSelect={selectEntity}
              />
            ) : (
              <MemoryEntityTable
                nodes={data?.nodes ?? []}
                hiddenTypes={hiddenTypes}
                query={search}
                sortKey={sortKey}
                onSelect={selectEntity}
              />
            )}
          </div>
        ) : null}

        {/* Search results panel (left side, slides over) — graph view only;
            cards/table filter their grid from the query directly. */}
        {effectiveView === "graph" && searchOpen && search.trim() ? (
          <aside className={cn(
            "absolute bottom-12 left-3 top-3 z-10 max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-lg border border-border/50 bg-card/95 shadow-lg backdrop-blur",
            compact ? "right-3 w-auto" : "w-80",
          )}>
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
                      const name = (isCanon ? r.headline || target : target).replace(
                        /^[a-z_]+:/,
                        "",
                      );
                      if (isCanon) {
                        // An entity_page hit IS that entity's own page —
                        // always drill the canvas to its ego neighbourhood
                        // (not just when off-cap), so search doubles as
                        // "jump to this node" the way a canvas click would.
                        void layers.enterEgo(target, name);
                      } else if (!simNodesRef.current.some((n) => n.id === target)) {
                        // Fragment hit: isolate only off-cap nodes (not in
                        // the current graph) — an in-graph hit just opens
                        // its panel.
                        isolateNode(target, 1);
                      }
                      const node =
                        simNodesRef.current.find((n) => n.id === target) ?? {
                          id: target,
                          type: target.split(":")[0] || "unknown",
                          name,
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

        {/* Edge popup — only reachable from the graph canvas. */}
        {effectiveView === "graph" && edgePopup ? (
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
              "absolute right-3 top-3 z-10 flex max-w-[calc(100vw-1.5rem)] flex-col rounded-lg border border-border/50 bg-card/95 text-sm shadow-lg backdrop-blur",
              "transition-[width] duration-200 ease-out",
              // Reserve space (split, graph re-fits) only in the desktop compact
              // panel. Expanded / mobile overlay the graph (no push) and fill the
              // full height.
              !compact && !panelExpanded && "mg-cpanel",
              compact || panelExpanded
                ? "bottom-3 w-[calc(100%-1.5rem)]"
                : "w-[22rem]",
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
                aria-label={
                  isolatedRef === selected.id
                    ? t("memoryGraph.backToFull")
                    : t("memoryGraph.isolate")
                }
                title={
                  isolatedRef === selected.id
                    ? t("memoryGraph.backToFull")
                    : t("memoryGraph.isolate")
                }
                onClick={() =>
                  isolatedRef === selected.id
                    ? exitIsolation()
                    : isolateNode(selected.id, isolateHops)
                }
                className={cn(
                  "h-6 w-6",
                  isolatedRef === selected.id && "bg-primary/10 text-primary",
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
                onClick={() => setSelected(null)}
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
                      {detail.page && (detail.page.derived_from?.length ?? 0) > 0 ? (
                        <div>
                          <dt className="text-muted-foreground">{t("memoryGraph.fieldSources")}</dt>
                          <dd className="mt-0.5 space-y-0.5">
                            {detail.page.derived_from!.map((ref) => (
                              <button
                                key={ref}
                                type="button"
                                onClick={() => {
                                  // The reference is not a graph node — open its
                                  // content in the side panel (same path as a
                                  // reference search hit).
                                  setSelected(null);
                                  if (tokenRef.current) {
                                    void fetchMemoryEntry(tokenRef.current, ref)
                                      .then((d) => setReferenceDetail(d))
                                      .catch(() => setReferenceDetail(null));
                                  }
                                }}
                                className="block w-full truncate rounded bg-muted px-1.5 py-0.5 text-left font-mono text-[10.5px] text-primary hover:bg-muted/70 hover:underline"
                              >
                                {ref.replace(/^reference:/, "")}
                              </button>
                            ))}
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
                        {stripHtmlComments(detail.page.body).trim()}
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
                                      : ev.kind === "derived_from"
                                        ? t("memoryGraph.provDerivedFrom")
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
            {!compact ? (
              <>
                <Separator className="bg-border/30" />
                <footer className="px-3 py-1.5 text-[10.5px] text-muted-foreground">
                  {t("memoryGraph.interactionHint")}
                </footer>
              </>
            ) : null}
          </aside>
        ) : null}

        {/* Reference content panel — references aren't graph nodes, so a
            reference search hit opens its rendered doc here. */}
        {referenceDetail && !selected ? (
          <aside
            className={cn(
              "mg-cpanel absolute right-3 top-3 z-10 flex max-w-[calc(100vw-1.5rem)] flex-col rounded-lg border border-border/50 bg-card/95 text-sm shadow-lg backdrop-blur",
              compact ? "w-[calc(100%-1.5rem)]" : "w-[min(58vw,44rem)]",
            )}
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
                  {stripHtmlComments(referenceDetail.body).trim()}
                </MarkdownTextRenderer>
              ) : (
                <p className="text-muted-foreground">{t("memoryGraph.noBody")}</p>
              )}
            </div>
          </aside>
        ) : null}

        {/* Hover preview (Obsidian page-preview): non-interactive popover with
            the hovered node's rendered body snippet. */}
        {effectiveView === "graph" && hoverPreview && hoverPreview.node.id !== selected?.id ? (
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
      </>
      )}
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
