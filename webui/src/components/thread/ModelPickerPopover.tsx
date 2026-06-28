import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronLeft, ChevronRight, Loader2, Search, Zap } from "lucide-react";
import { useTranslation } from "react-i18next";

import { fetchModelPicker, type PickerEntry } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

/** Reasoning-effort levels. Uniform across reasoning-capable models today; the
 *  drill-in only appears when the active model supports reasoning. */
export const EFFORT_LEVELS = [
  { value: "", labelKey: "thread.composer.effort.default" },
  { value: "none", labelKey: "thread.composer.effort.off" },
  { value: "high", labelKey: "thread.composer.effort.high" },
  { value: "max", labelKey: "thread.composer.effort.max" },
] as const;

export function effortLabelKey(activeEffort: string | null | undefined): string {
  const match = EFFORT_LEVELS.find((l) => l.value === (activeEffort ?? ""));
  return (match ?? EFFORT_LEVELS[0]).labelKey;
}

const RECENTS_KEY = "durin.recentModels";

function readRecents(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function pushRecent(model: string) {
  const next = [model, ...readRecents().filter((m) => m !== model)].slice(0, 8);
  localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
}

function formatCtx(n?: number | null): string | null {
  if (!n) return null;
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(n);
}

function capLine(e: PickerEntry, t: (k: string) => string): string {
  const parts: string[] = [];
  const ctx = formatCtx(e.max_input_tokens);
  if (ctx) parts.push(ctx);
  if (e.supports_vision) parts.push(t("thread.composer.modelPicker.capVision"));
  if (e.supports_audio_input) parts.push(t("thread.composer.modelPicker.capAudio"));
  if (e.supports_reasoning) parts.push(t("thread.composer.modelPicker.capReasoning"));
  return parts.join(" · ");
}

interface ModelPickerPopoverProps {
  open: boolean;
  onClose: () => void;
  onSelect: (ref: string) => void;
  activeModel: string | null;
  /** When set, the popover is portaled to <body> and positioned `fixed` against
   *  this element, so it escapes overflow-clipping ancestors (e.g. the scrollable
   *  settings panel). Without it, the popover stays absolutely positioned. */
  anchorRef?: React.RefObject<HTMLElement | null>;
  /** Whether the active model supports reasoning. Gates the effort drill-in:
   *  with no reasoning, the effort row is hidden entirely. */
  canReason?: boolean;
  /** Active reasoning effort (drives the drill-in's current label/selection). */
  activeEffort?: string | null;
  /** Pick a reasoning effort. When present (and `canReason`), the popover shows
   *  an "Effort ›" drill-in scoped to the active model. */
  onEffortSelect?: (effort: string) => void;
}

export function ModelPickerPopover({
  open,
  onClose,
  onSelect,
  activeModel,
  anchorRef,
  canReason = false,
  activeEffort = null,
  onEffortSelect,
}: ModelPickerPopoverProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [query, setQuery] = useState("");
  const [entries, setEntries] = useState<PickerEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [page, setPage] = useState<"models" | "effort">("models");
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number } | null>(null);
  const showEffort = !!onEffortSelect && canReason;

  useEffect(() => {
    if (!open || loaded) return;
    setLoading(true);
    fetchModelPicker(token, readRecents())
      .then((rows) => {
        setEntries(rows);
        setLoaded(true);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [open, loaded, token]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setPage("models");
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  // When anchored, position the portaled popover `fixed` against the trigger,
  // flipping below it when there isn't room above. Recompute on scroll/resize.
  useLayoutEffect(() => {
    if (!open || !anchorRef?.current) return;
    const compute = () => {
      const el = anchorRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const WIDTH = 340, GAP = 8, EST_HEIGHT = 360, MARGIN = 16;
      const left = Math.max(MARGIN, Math.min(r.left, window.innerWidth - WIDTH - MARGIN));
      const spaceAbove = r.top;
      const spaceBelow = window.innerHeight - r.bottom;
      if (spaceAbove >= EST_HEIGHT || spaceAbove >= spaceBelow) {
        setPos({ left, bottom: window.innerHeight - r.top + GAP });
      } else {
        setPos({ left, top: r.bottom + GAP });
      }
    };
    compute();
    window.addEventListener("scroll", compute, true);
    window.addEventListener("resize", compute);
    return () => {
      window.removeEventListener("scroll", compute, true);
      window.removeEventListener("resize", compute);
    };
  }, [open, anchorRef]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (containerRef.current?.contains(target)) return;
      if (anchorRef?.current?.contains(target)) return;
      onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open, onClose, anchorRef]);

  // Group filtered entries by their section, preserving server order (the
  // "Easy pick" block is emitted first, then one block per configured provider).
  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = q
      ? entries.filter(
          (e) => e.name.toLowerCase().includes(q) || e.provider.toLowerCase().includes(q),
        )
      : entries;
    const out: { group: string; items: PickerEntry[] }[] = [];
    for (const e of matched) {
      const last = out[out.length - 1];
      if (last && last.group === e.group) last.items.push(e);
      else out.push({ group: e.group, items: [e] });
    }
    return out;
  }, [query, entries]);

  if (!open) return null;

  const handleSelect = (entry: PickerEntry) => {
    pushRecent(entry.name);
    onSelect(entry.ref);
    onClose();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const trimmed = query.trim();
      if (trimmed) {
        onSelect(trimmed);
        onClose();
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  const empty = !loading && groups.length === 0;

  const panel = (
    <div
      ref={containerRef}
      style={
        anchorRef
          ? { position: "fixed", left: pos?.left, top: pos?.top, bottom: pos?.bottom, visibility: pos ? "visible" : "hidden" }
          : undefined
      }
      className={cn(
        "z-50 w-[340px] max-w-[calc(100vw-2rem)]",
        anchorRef ? "fixed" : "absolute bottom-full left-0 mb-2 slide-in-from-bottom-2",
        "rounded-xl border border-border/70 bg-popover shadow-xl",
        "animate-in fade-in-0 zoom-in-95 duration-200",
      )}
    >
      {page === "models" ? (
        <>
          <div className="flex items-center gap-2 border-b border-border/50 px-3 py-2.5">
            <Search className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={t("thread.composer.modelPicker.search")}
              className="flex-1 bg-transparent text-[13px] text-foreground placeholder:text-muted-foreground focus:outline-none"
            />
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" /> : null}
          </div>
          <div className="max-h-[280px] overflow-y-auto py-1">
            {groups.map((g) => (
              <div key={g.group}>
                <p className="px-3 py-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground/70">
                  {g.group}
                </p>
                {g.items.map((entry) => (
                  <ModelRow
                    key={`${entry.provider}:${entry.name}`}
                    entry={entry}
                    active={entry.name === activeModel}
                    onSelect={handleSelect}
                  />
                ))}
              </div>
            ))}
            {empty ? (
              <div className="px-3 py-4 text-center text-[12px] text-muted-foreground">
                {query.trim()
                  ? t("thread.composer.modelPicker.noMatch")
                  : t("thread.composer.modelPicker.empty")}
              </div>
            ) : null}
          </div>
          {showEffort ? (
            <button
              type="button"
              onClick={() => setPage("effort")}
              className="flex w-full items-center justify-between gap-2 border-t border-border/50 px-3 py-2.5 text-[12.5px] text-foreground/85 transition-colors hover:bg-muted/60"
            >
              <span className="flex items-center gap-2">
                <Zap className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
                {t("thread.composer.effort.title")}
              </span>
              <span className="flex items-center gap-1 text-muted-foreground">
                <span>{t(effortLabelKey(activeEffort))}</span>
                <ChevronRight className="h-3.5 w-3.5" aria-hidden />
              </span>
            </button>
          ) : null}
        </>
      ) : (
        <>
          <button
            type="button"
            onClick={() => setPage("models")}
            className="flex w-full items-center gap-2 border-b border-border/50 px-3 py-2.5 text-[12.5px] font-medium text-foreground/85 transition-colors hover:bg-muted/60"
          >
            <ChevronLeft className="h-4 w-4 text-muted-foreground" aria-hidden />
            {t("thread.composer.effort.title")}
          </button>
          <div className="py-1">
            {EFFORT_LEVELS.map((level) => {
              const active = level.value === (activeEffort ?? "");
              return (
                <button
                  key={level.value}
                  type="button"
                  onClick={() => {
                    onEffortSelect?.(level.value);
                    onClose();
                  }}
                  className={cn(
                    "flex w-full items-center justify-between px-3 py-1.5 text-left text-[12.5px] transition-colors",
                    active
                      ? "bg-accent/60 font-medium text-accent-foreground"
                      : "text-foreground/85 hover:bg-muted/60",
                  )}
                >
                  <span>{t(level.labelKey)}</span>
                  {active ? <span className="text-[10px] text-emerald-500">●</span> : null}
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );

  return anchorRef ? createPortal(panel, document.body) : panel;
}

function ModelRow({
  entry,
  active,
  onSelect,
}: {
  entry: PickerEntry;
  active: boolean;
  onSelect: (entry: PickerEntry) => void;
}) {
  const { t } = useTranslation();
  const caps = capLine(entry, t);
  return (
    <button
      type="button"
      onClick={() => onSelect(entry)}
      className={cn(
        "flex w-full flex-col gap-0.5 px-3 py-1.5 text-left transition-colors",
        active
          ? "bg-accent/60 font-medium text-accent-foreground"
          : "text-foreground/85 hover:bg-muted/60",
      )}
    >
      <span className="flex w-full items-center gap-2 text-[12.5px]">
        <span className="min-w-0 flex-1 truncate">{entry.name}</span>
        <span className="flex-none text-[10px] text-muted-foreground/70">{entry.provider}</span>
        {active ? <span className="flex-none text-[10px] text-emerald-500">●</span> : null}
      </span>
      {caps ? (
        <span className="text-[10px] text-muted-foreground/60">{caps}</span>
      ) : null}
    </button>
  );
}
