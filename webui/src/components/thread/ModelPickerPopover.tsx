import { useEffect, useMemo, useRef, useState } from "react";
import { Search, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { fetchModelPicker, type PickerEntry } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

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
}

export function ModelPickerPopover({ open, onClose, onSelect, activeModel }: ModelPickerPopoverProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [query, setQuery] = useState("");
  const [entries, setEntries] = useState<PickerEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

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
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open, onClose]);

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

  return (
    <div
      ref={containerRef}
      className={cn(
        "absolute bottom-full left-0 z-50 mb-2 w-[340px] max-w-[calc(100vw-2rem)]",
        "rounded-xl border border-border/70 bg-popover shadow-xl",
        "animate-in fade-in-0 zoom-in-95 slide-in-from-bottom-2 duration-200",
      )}
    >
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
    </div>
  );
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
