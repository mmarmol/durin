import { useEffect, useMemo, useRef, useState } from "react";
import { Search, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { listModels } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

interface ModelPickerPopoverProps {
  open: boolean;
  onClose: () => void;
  onSelect: (model: string) => void;
  activeModel: string | null;
}

export function ModelPickerPopover({ open, onClose, onSelect, activeModel }: ModelPickerPopoverProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [query, setQuery] = useState("");
  const [suggested, setSuggested] = useState<string[]>([]);
  const [allModels, setAllModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || loaded) return;
    setLoading(true);
    listModels(token, "")
      .then((cat) => {
        setSuggested(cat.suggested ?? []);
        setAllModels(cat.models ?? []);
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

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return { suggested: suggested.slice(0, 10), rest: [] as string[] };
    const matches = allModels.filter((m) => m.toLowerCase().includes(q)).slice(0, 50);
    const suggestedMatches = suggested.filter((m) => m.toLowerCase().includes(q));
    return { suggested: suggestedMatches.slice(0, 8), rest: matches };
  }, [query, suggested, allModels]);

  if (!open) return null;

  const handleSelect = (model: string) => {
    onSelect(model);
    onClose();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const trimmed = query.trim();
      if (trimmed) handleSelect(trimmed);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

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
        {filtered.suggested.length > 0 ? (
          <>
            <p className="px-3 py-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground/70">
              {t("thread.composer.modelPicker.suggested")}
            </p>
            {filtered.suggested.map((m) => (
              <ModelRow key={m} model={m} active={m === activeModel} onSelect={handleSelect} />
            ))}
          </>
        ) : null}
        {filtered.rest.length > 0 ? (
          <>
            {filtered.suggested.length > 0 ? (
              <div className="mx-3 my-1 border-t border-border/40" />
            ) : null}
            <p className="px-3 py-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground/70">
              {t("thread.composer.modelPicker.all")}
            </p>
            {filtered.rest.map((m) => (
              <ModelRow key={m} model={m} active={m === activeModel} onSelect={handleSelect} />
            ))}
          </>
        ) : null}
        {!loading && filtered.suggested.length === 0 && filtered.rest.length === 0 ? (
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
  model,
  active,
  onSelect,
}: {
  model: string;
  active: boolean;
  onSelect: (m: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(model)}
      className={cn(
        "flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] transition-colors",
        active
          ? "bg-accent/60 font-medium text-accent-foreground"
          : "text-foreground/85 hover:bg-muted/60",
      )}
    >
      <span className="min-w-0 flex-1 truncate">{model}</span>
      {active ? (
        <span className="flex-none text-[10px] text-emerald-500">●</span>
      ) : null}
    </button>
  );
}
