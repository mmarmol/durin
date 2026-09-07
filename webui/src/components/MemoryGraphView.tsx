import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import {
  ArrowDownUp,
  BookOpen,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  LayoutGrid,
  Maximize2,
  Minimize2,
  Network,
  RefreshCw,
  Search as SearchIcon,
  Table2,
  Trash2,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { DocumentsShelf } from "@/components/DocumentsShelf";
import { useMemoryGraph } from "@/hooks/useMemoryGraph";
import { useClient } from "@/providers/ClientProvider";
import MarkdownTextRenderer from "@/components/MarkdownTextRenderer";
import {
  ApiError,
  fetchMemoryBacklinks,
  fetchMemoryEntity,
  fetchMemoryEntry,
  fetchMemorySession,
  forgetMemoryEntry,
  searchMemoryApi,
  type MemoryBacklinksPayload,
  type MemoryEntityDetail,
  type MemoryEntryDetail,
  type MemoryGraphNode,
  type MemorySearchPayload,
  type MemorySearchResult,
  type MemorySessionDetail,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  browseEntities,
  colorForType,
  groupTypeLegend,
  type EntitySortKey,
} from "@/lib/memory-graph-style";
import { EntityMiniGraph } from "@/components/EntityMiniGraph";
import { MemoryEntityCards } from "@/components/MemoryEntityCards";
import { MemoryEntityTable } from "@/components/MemoryEntityTable";
import { MemoryTypeFilter } from "@/components/MemoryTypeFilter";

interface MemoryGraphViewProps {
  active: boolean;
  onToggleSidebar?: () => void;
  hideSidebarToggleOnDesktop?: boolean;
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

// Node kinds the entity inventory never lists: sessions have their own
// detail panel (reached from an entity's provenance), references live in the
// Documents tab. They stay in the payload as edge endpoints only, so they
// must not surface as type-filter rows either — a row for a type no
// presentation can show would be a dead control.
const NON_ENTITY_TYPES = new Set(["session", "reference"]);

type TabName = "info" | "body" | "provenance" | "history" | "sources" | "archive" | "entries";
type SessionTabName = "info" | "messages" | "events" | "memory_ops" | "entries";

export function MemoryGraphView(_props: MemoryGraphViewProps) {
  const { t } = useTranslation();
  const { data, loading, error, refresh } = useMemoryGraph(_props.active);
  // Reference docs (memory/references/*) aren't entities; a reference opened
  // from an entity's "Source documents" shows its content in this side panel.
  const [referenceDetail, setReferenceDetail] = useState<MemoryEntryDetail | null>(null);
  const { token } = useClient();
  const tokenRef = useRef(token);
  tokenRef.current = token;
  // Two content domains under one memory page: the entity inventory and the
  // Library shelf of ingested reference documents. Presentation of the
  // entities (table / cards) is a separate axis below — two views of the
  // same set, not sibling tabs.
  const [mode, setMode] = useState<"entities" | "documents">("entities");
  // Entities presentation, persisted. The table (sorted by recent activity)
  // is the first-run default. The stored value is read under the versioned
  // key first and the pre-versioning key second; anything that isn't a
  // presentation this view still offers — including the retired graph
  // canvas an older install may have stored — falls back to the table.
  const [view, setView] = useState<"cards" | "table">(() => {
    try {
      const stored =
        localStorage.getItem("durin.memoryGraph.view.v2") ??
        localStorage.getItem("durin.memoryGraph.view");
      return stored === "cards" ? "cards" : "table";
    } catch {
      /* localStorage unavailable */
    }
    return "table";
  });
  const setViewPersisted = useCallback((v: "cards" | "table") => {
    setView(v);
    try {
      localStorage.setItem("durin.memoryGraph.view.v2", v);
    } catch {
      /* localStorage unavailable: ephemeral choice is fine */
    }
  }, []);
  // Shared ordering for the cards presentation (the table sorts by column).
  const [sortKey, setSortKey] = useState<EntitySortKey>("recent");

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
  const isSessionSelected = selected?.type === "session";
  // Wide-mode toggle for the right-hand detail panel. Sessions can
  // accumulate long tool outputs that get cramped in the default
  // ~22rem column; expand to the full width when the user needs to read
  // full message bodies. Persisted so the choice survives reloads.
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

  // Set of node types the user has toggled OFF in the type filter. Phantom
  // (unconsolidated mentions) is hidden by default so a fresh view opens on
  // the consolidated entities; clicking a row flips inclusion. Phantom is
  // treated as its own pseudo-type (not a `type` field value).
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(
    new Set(["phantom"]),
  );

  function toggleType(type: string): void {
    setHiddenTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  // Live filter for the table/cards grid (name / alias / summary substring,
  // applied client-side from the loaded payload — no backend round-trip).
  const [search, setSearch] = useState("");

  // Mobile: panels go full-screen — one surface at a time.
  const [compact, setCompact] = useState(
    () => typeof window !== "undefined" && window.innerWidth < 720,
  );
  useEffect(() => {
    const onResize = () => setCompact(window.innerWidth < 720);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Everything the inventory could list, before the user's filters and
  // search — the honest "is there anything here at all" count. Phantoms are
  // included: a workspace whose only entities are still unconsolidated is
  // not empty, it's filtered (the default phantom toggle hides them), and
  // the presentations say so themselves.
  const entityCount = useMemo(
    () =>
      data
        ? browseEntities(data.nodes, {
            hiddenTypes: new Set(),
            query: "",
            sortKey: "name",
          }).length
        : 0,
    [data],
  );

  // Exactly one thing occupies the main pane at a time. The presentations
  // (table/cards) only mount once the payload has entities, so their own
  // "nothing matches your filters" state can never stack on top of a
  // loading or empty-workspace message — the two used to render together
  // as absolute overlays and read as contradictory explanations.
  const paneState: "error" | "loading" | "empty" | "list" | "idle" = error
    ? "error"
    : data == null
      ? loading
        ? "loading"
        : "idle"
      : entityCount === 0
        ? "empty"
        : "list";

  // Navigate to the session a fact came from (provenance source_ref →
  // `session:<stem>` node). Selecting the node opens the session detail
  // panel via the existing `selected` effect. No-op when the session node
  // isn't in the payload (e.g. its .jsonl was removed).
  const selectSessionByStem = useCallback(
    (stem: string, targetTs?: string | null) => {
      const id = `session:${stem}`;
      const node = data?.nodes.find((n) => n.id === id);
      if (!node) return;
      setSelected(node);
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

  // Fetch detail whenever the selection changes — branch by type.
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setSessionDetail(null);
      setDetailError(null);
      return;
    }
    // Reference nodes aren't entities — they have no entity-detail endpoint.
    // Hand off to the reference panel: clear the selection and load the
    // document into `referenceDetail`.
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

  const typesLegend = useMemo(() => {
    if (!data) return [] as { type: string; color: string; count: number }[];
    const counts = new Map<string, number>();
    for (const n of data.nodes) counts.set(n.type, (counts.get(n.type) ?? 0) + 1);
    return data.stats.types
      .filter((ty) => !NON_ENTITY_TYPES.has(ty))
      .map((ty) => ({
        type: ty,
        color: colorForType(ty),
        count: counts.get(ty) ?? 0,
      }));
  }, [data]);

  // Cap the filter popover to the top N types by count; the rest collapse
  // into a single "others (N)" row (see groupTypeLegend) instead of growing
  // the list unboundedly as a workspace's open-vocabulary type set expands.
  const { shown: shownTypesLegend, tail: tailTypesLegend } = useMemo(
    () => groupTypeLegend(typesLegend),
    [typesLegend],
  );

  // Hide every type at once (the "start from nothing, reveal one" flow);
  // phantom is a pseudo-type toggle.
  const hideAllTypes = useCallback(() => {
    if (!data) return;
    const next = new Set<string>(typesLegend.map((tl) => tl.type));
    if (data.stats.phantom_count > 0) next.add("phantom");
    setHiddenTypes(next);
  }, [data, typesLegend]);

  // Solo: show only `type` (hide all others, and phantom unless it is the solo).
  const soloType = useCallback(
    (type: string) => {
      if (!data) return;
      const next = new Set<string>(
        typesLegend.map((tl) => tl.type).filter((ty) => ty !== type),
      );
      if (data.stats.phantom_count > 0 && type !== "phantom") next.add("phantom");
      setHiddenTypes(next);
    },
    [data, typesLegend],
  );

  // Select an entity from the cards grid or the table.
  const selectEntity = useCallback((n: MemoryGraphNode) => {
    setSelected(n);
    setPanelExpanded(false);
    setActiveTab(n.phantom ? "info" : "body");
    setReferenceDetail(null);
  }, []);

  // Open an entity's page by ref — from the Documents shelf's entity list or
  // the panel's Related neighbours. The payload is capped, so a ref may not
  // be in it: a placeholder node still opens the panel, and the detail fetch
  // (keyed by ref) fills it in either way.
  const handleOpenEntity = useCallback(
    (ref: string) => {
      setMode("entities");
      setReferenceDetail(null);
      const node =
        data?.nodes.find((n) => n.id === ref) ?? {
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
    [data],
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
            {t("memoryGraph.entitiesTotal", { count: entityCount })}
            {data.stats.phantom_count > 0
              ? ` · ${t("memoryGraph.statsPhantom", { count: data.stats.phantom_count })}`
              : ""}
            {data.stats.truncated_nodes || data.stats.truncated_edges
              ? ` · ${t("memoryGraph.statsTruncated")}`
              : ""}
          </span>
        ) : null}
        <div className="ml-auto flex min-w-0 items-center gap-2">
          <div className={cn("relative", compact && "min-w-0 flex-1")}>
            <SearchIcon
              className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
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
                onClick={() => setSearch("")}
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
            onClick={() => void refresh()}
            disabled={loading}
            className="h-7 w-7"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          </Button>
        </div>
          </>
        ) : null}
      </header>

      {/* Entities toolbar — the view switcher (two presentations of the same
          entity set, Table/Cards), the type filter (shared by both), and the
          sort control for the cards grid. */}
      {mode === "entities" ? (
        <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-border/40 px-3 py-1.5 text-[11px]">
          <div className="flex items-center gap-0.5 rounded-md border border-border/50 p-0.5">
            <button
              type="button"
              onClick={() => setViewPersisted("table")}
              className={cn(
                "flex items-center gap-1 rounded px-2 py-0.5 transition-colors",
                view === "table"
                  ? "bg-primary/10 font-medium text-primary"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
            >
              <Table2 className="h-3 w-3" /> {t("memoryGraph.viewTable")}
            </button>
            <button
              type="button"
              onClick={() => setViewPersisted("cards")}
              className={cn(
                "flex items-center gap-1 rounded px-2 py-0.5 transition-colors",
                view === "cards"
                  ? "bg-primary/10 font-medium text-primary"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
            >
              <LayoutGrid className="h-3 w-3" /> {t("memoryGraph.viewCards")}
            </button>
          </div>
          {typesLegend.length > 0 || (data?.stats.phantom_count ?? 0) > 0 ? (
            <span className="mx-0.5 h-4 w-px bg-border/60" aria-hidden />
          ) : null}
          <MemoryTypeFilter
            types={shownTypesLegend}
            tail={tailTypesLegend}
            phantomCount={data?.stats.phantom_count ?? 0}
            hidden={hiddenTypes}
            onToggle={toggleType}
            onShowAll={() => setHiddenTypes(new Set())}
            onHideAll={hideAllTypes}
            onSolo={soloType}
          />
          {view === "cards" ? (
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
      <div className="relative min-h-0 flex-1">
        {paneState === "error" ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-sm text-destructive">
            <span>{error}</span>
            <Button variant="outline" size="sm" onClick={() => void refresh()}>
              {t("memoryGraph.retry")}
            </Button>
          </div>
        ) : null}
        {paneState === "loading" ? (
          <div
            role="status"
            className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground"
          >
            {t("memoryGraph.loading")}
          </div>
        ) : null}
        {paneState === "empty" ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 px-8 text-center">
            <Network className="h-10 w-10 text-muted-foreground/40" aria-hidden />
            <p className="text-sm font-medium text-foreground">{t("memoryGraph.emptyTitle")}</p>
            <p className="max-w-sm text-xs text-muted-foreground">{t("memoryGraph.emptyBody")}</p>
            <p className="max-w-sm text-xs text-muted-foreground">
              <Trans
                i18nKey="memoryGraph.emptyHint"
                components={{ code: <code className="rounded bg-muted px-1" /> }}
              />
            </p>
          </div>
        ) : null}
        {paneState === "list" && data ? (
          // Cards / table presentations. When the desktop compact detail
          // panel is open it reserves the right-hand column, so the grid
          // re-flows beside it instead of hiding rows underneath.
          <div
            className="absolute inset-0"
            style={
              selected && !compact && !panelExpanded
                ? { paddingRight: "23rem" }
                : undefined
            }
          >
            {view === "cards" ? (
              <MemoryEntityCards
                nodes={data.nodes}
                hiddenTypes={hiddenTypes}
                query={search}
                sortKey={sortKey}
                onSelect={selectEntity}
              />
            ) : (
              <MemoryEntityTable
                nodes={data.nodes}
                hiddenTypes={hiddenTypes}
                query={search}
                sortKey={sortKey}
                onSelect={selectEntity}
              />
            )}
          </div>
        ) : null}

        {/* Right-side detail panel for the selected entity or session. The
            desktop compact panel sits beside the grid; expanded / mobile
            overlay it and fill the full height. */}
        {selected ? (
          <aside
            className={cn(
              "absolute right-3 top-3 z-10 flex max-w-[calc(100vw-1.5rem)] flex-col rounded-lg border border-border/50 bg-card/95 text-sm shadow-lg backdrop-blur",
              "transition-[width] duration-200 ease-out",
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
                    <>
                      {/* The related-entities mini-graph, real entities
                          only — phantom/session/reference nodes either have
                          no meaningful neighbourhood (phantom) or are
                          handled by an entirely different panel (session,
                          reference; the latter never reaches this branch —
                          see the "reference" redirect above — kept here
                          anyway as a defensive, self-documenting guard). */}
                      {!selected.phantom &&
                      selected.type !== "session" &&
                      selected.type !== "reference" ? (
                        <EntityMiniGraph
                          token={token}
                          entityRef={selected.id}
                          entityName={selected.name}
                          onNavigate={(ref) => handleOpenEntity(ref)}
                        />
                      ) : null}
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
                                  // The reference is not an entity — open its
                                  // content in the side panel.
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
                    </>
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
          </aside>
        ) : null}

        {/* Reference content panel — references aren't entities, so a
            source document opens its rendered doc here. */}
        {referenceDetail && !selected ? (
          <aside
            className={cn(
              "absolute right-3 top-3 z-10 flex max-w-[calc(100vw-1.5rem)] flex-col rounded-lg border border-border/50 bg-card/95 text-sm shadow-lg backdrop-blur",
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
      </div>
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
