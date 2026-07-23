import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, Eye, EyeOff, Filter, Search as SearchIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { type TypeLegendItem } from "@/lib/memory-graph-style";

// Re-exported so existing importers (this type used to live here) keep working.
export type { TypeLegendItem };

interface MemoryTypeFilterProps {
  /** Real entity types, already sorted, with their color + node count. */
  types: TypeLegendItem[];
  /** Long-tail types grouped behind one "others (N)" row (see
   *  `groupTypeLegend`); each still toggles individually via `onToggle`. */
  tail?: TypeLegendItem[];
  /** Phantom node count; when > 0 a `phantom` pseudo-type row is offered. */
  phantomCount: number;
  /** Zero-edge node count; when > 0 a `disconnected` pseudo-type row is
   *  offered, labeled via i18n (unlike phantom, "disconnected" isn't a
   *  real type name, so it has no literal string of its own to display). */
  disconnectedCount?: number;
  /** Currently hidden types (may include the `phantom`/`disconnected`
   *  pseudo-types). */
  hidden: Set<string>;
  onToggle: (type: string) => void;
  onShowAll: () => void;
  onHideAll: () => void;
  onSolo: (type: string) => void;
}

/** Scalable replacement for the inline type-filter chips: a compact
 *  "Types (N visible)" trigger that opens a searchable popover with per-type
 *  visibility toggles + counts, and Show all / Hide all / Solo. Unlike the
 *  chip row it keeps a fixed toolbar height as the (open-vocabulary) type set
 *  grows, and it is where "hide everything, then reveal one" lives. */
export function MemoryTypeFilter({
  types,
  tail = [],
  phantomCount,
  disconnectedCount = 0,
  hidden,
  onToggle,
  onShowAll,
  onHideAll,
  onSolo,
}: MemoryTypeFilterProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      setOpen(false);
      // Consume the keypress: MemoryGraphView also listens for Escape
      // (to back out of a graph drill) on `window`, which — in the bubble
      // phase — fires AFTER this `document` listener. Marking it prevented
      // here lets that later handler check `e.defaultPrevented` and skip
      // its own action, so one Escape press closes only the top-most layer
      // (this popover) instead of also exiting the drill underneath it.
      e.preventDefault();
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const hasPhantom = phantomCount > 0;
  const hasDisconnected = disconnectedCount > 0;
  const visibleCount =
    types.filter((tl) => !hidden.has(tl.type)).length +
    tail.filter((tl) => !hidden.has(tl.type)).length +
    (hasPhantom && !hidden.has("phantom") ? 1 : 0) +
    (hasDisconnected && !hidden.has("disconnected") ? 1 : 0);

  const needle = query.trim().toLowerCase();
  const filtered = useMemo(
    () => (needle ? types.filter((tl) => tl.type.includes(needle)) : types),
    [types, needle],
  );
  const showPhantomRow = hasPhantom && (!needle || "phantom".includes(needle));
  const disconnectedLabel = t("memoryGraph.typeDisconnected");
  const showDisconnectedRow =
    hasDisconnected && (!needle || disconnectedLabel.toLowerCase().includes(needle));

  if (types.length === 0 && tail.length === 0 && !hasPhantom && !hasDisconnected) return null;

  function row(
    type: string,
    color: string | null,
    count: number | null,
    label: string = type,
  ) {
    const isHidden = hidden.has(type);
    // Row = two sibling buttons (never nested — invalid + ambiguous a11y name):
    // a wide toggle for show/hide, and a narrow "Only" for solo.
    return (
      <div key={type} className="group flex items-center gap-1 rounded pr-1 hover:bg-muted">
        <button
          type="button"
          onClick={() => onToggle(type)}
          aria-pressed={!isHidden}
          className="flex flex-1 items-center gap-2 rounded px-1.5 py-1 text-left"
        >
          {color === null ? (
            <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full border border-dashed border-foreground/50" />
          ) : (
            <span
              className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
              style={{ background: color }}
            />
          )}
          <span
            className={cn(
              "flex-1 truncate",
              isHidden && "text-muted-foreground line-through",
            )}
          >
            {label}
          </span>
          {count !== null ? (
            <span className="tabular-nums text-[10px] text-muted-foreground">{count}</span>
          ) : null}
          {isHidden ? (
            <EyeOff className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
          ) : (
            <Eye className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
          )}
        </button>
        <button
          type="button"
          onClick={() => onSolo(type)}
          aria-label={`${t("memoryGraph.onlyType")} ${label}`}
          className="rounded px-1 py-0.5 text-[10px] text-primary opacity-0 hover:bg-primary/10 focus:opacity-100 group-hover:opacity-100"
        >
          {t("memoryGraph.onlyType")}
        </button>
      </div>
    );
  }

  // Single row standing in for every grouped tail type: a neutral (dashed,
  // colorless) swatch since it spans several real types, and one toggle
  // that flips all of them at once. No "Only" button — soloing a group of
  // types isn't a defined action here. `aria-pressed` reflects "all tail
  // types are currently visible"; a mixed state (some hidden, some not)
  // reads as not-pressed the same as fully hidden — there's no third,
  // partial visual state.
  function tailRow() {
    const allVisible = tail.every((tl) => !hidden.has(tl.type));
    return (
      <div key="__tail__" className="group flex items-center gap-1 rounded pr-1 hover:bg-muted">
        <button
          type="button"
          onClick={() => tail.forEach((tl) => onToggle(tl.type))}
          aria-pressed={allVisible}
          className="flex flex-1 items-center gap-2 rounded px-1.5 py-1 text-left"
        >
          <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-full border border-dashed border-foreground/50" />
          <span
            className={cn(
              "flex-1 truncate",
              !allVisible && "text-muted-foreground line-through",
            )}
          >
            {t("memoryGraph.typesOthers", { count: tail.length })}
          </span>
          {allVisible ? (
            <Eye className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
          ) : (
            <EyeOff className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
          )}
        </button>
      </div>
    );
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="true"
        aria-expanded={open}
        className={cn(
          "flex items-center gap-1 rounded border px-2 py-0.5 transition-colors hover:bg-muted",
          hidden.size > 0
            ? "border-primary/40 text-primary"
            : "border-border/50 text-muted-foreground",
        )}
      >
        <Filter className="h-3 w-3" aria-hidden />
        <span>{t("memoryGraph.filterTypes")}</span>
        <span className="text-[10px] opacity-70">
          {t("memoryGraph.typesVisibleCount", { count: visibleCount })}
        </span>
        <ChevronDown className="h-3 w-3" aria-hidden />
      </button>

      {open ? (
        <div className="absolute left-0 top-full z-30 mt-1 w-60 rounded-lg border border-border/50 bg-card/95 p-1.5 text-[11px] shadow-lg backdrop-blur">
          <div className="relative mb-1">
            <SearchIcon
              className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("memoryGraph.searchTypesPlaceholder")}
              className="h-7 w-full rounded-md border border-input bg-background pl-7 pr-2 text-[12px] outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div className="max-h-56 overflow-y-auto">
            {filtered.length === 0 && !showPhantomRow && !showDisconnectedRow && tail.length === 0 ? (
              <div className="px-1.5 py-2 text-center text-muted-foreground">
                {t("memoryGraph.noMatches")}
              </div>
            ) : (
              <>
                {filtered.map((tl) => row(tl.type, tl.color, tl.count))}
                {/* Not subject to the search box: the tail is a fixed
                    summary row, not an individually-searchable type. */}
                {tail.length > 0 ? tailRow() : null}
                {showPhantomRow ? row("phantom", null, phantomCount) : null}
                {showDisconnectedRow
                  ? row("disconnected", null, disconnectedCount, disconnectedLabel)
                  : null}
              </>
            )}
          </div>
          <div className="mt-1 flex gap-1 border-t border-border/40 pt-1.5">
            <button
              type="button"
              onClick={onShowAll}
              className="flex flex-1 items-center justify-center gap-1 rounded border border-border/40 px-1.5 py-1 text-[10px] text-muted-foreground hover:bg-muted"
            >
              <Eye className="h-3 w-3" aria-hidden /> {t("memoryGraph.showAll")}
            </button>
            <button
              type="button"
              onClick={onHideAll}
              className="flex flex-1 items-center justify-center gap-1 rounded border border-border/40 px-1.5 py-1 text-[10px] text-muted-foreground hover:bg-muted"
            >
              <EyeOff className="h-3 w-3" aria-hidden /> {t("memoryGraph.hideAll")}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
