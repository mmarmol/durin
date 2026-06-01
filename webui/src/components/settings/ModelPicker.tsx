import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Input } from "@/components/ui/input";
import { listModels } from "@/lib/api";
import { cn } from "@/lib/utils";

/** Searchable model field: type freely, or pick from the provider's
 *  catalog. The curated per-provider shortlist floats to the top.
 *
 *  ``capability``: optional ``"vision" | "audio" | "text"``. When set,
 *  the backend filters the catalog so the picker only surfaces models
 *  that support the requested modality — used by the vision/audio aux
 *  pickers to stop suggesting text-only models.
 */
export function ModelPicker({
  token,
  provider,
  value,
  onChange,
  capability = "",
}: {
  token: string;
  provider: string;
  value: string;
  onChange: (model: string) => void;
  capability?: string;
}) {
  const { t } = useTranslation();
  const [suggested, setSuggested] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    listModels(token, provider, capability)
      .then((catalog) => {
        if (cancelled) return;
        setSuggested(catalog.suggested);
        setModels(catalog.models);
      })
      .catch(() => {
        if (cancelled) return;
        setSuggested([]);
        setModels([]);
      });
    return () => {
      cancelled = true;
    };
  }, [token, provider, capability]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const query = value.trim().toLowerCase();
  const filtered = useMemo(() => {
    const seen = new Set<string>();
    const out: Array<{ id: string; suggested: boolean }> = [];
    for (const m of suggested) {
      if (!query || m.toLowerCase().includes(query)) {
        out.push({ id: m, suggested: true });
        seen.add(m);
      }
    }
    for (const m of models) {
      if (seen.has(m)) continue;
      if (!query || m.toLowerCase().includes(query)) {
        out.push({ id: m, suggested: false });
      }
      if (out.length >= 40) break;
    }
    return out;
  }, [suggested, models, query]);

  return (
    <div ref={boxRef} className="relative">
      <Input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
        }}
        placeholder={t("settings.models.modelPlaceholder")}
        className="h-8 w-[280px] rounded-full text-[13px]"
      />
      {open && filtered.length > 0 ? (
        <div className="absolute right-0 z-20 mt-1 max-h-[260px] w-[280px] overflow-y-auto rounded-[16px] border border-border/50 bg-popover p-1 shadow-[0_18px_50px_rgba(15,23,42,0.16)]">
          {filtered.map(({ id, suggested: isSuggested }) => (
            <button
              key={id}
              type="button"
              onClick={() => {
                onChange(id);
                setOpen(false);
              }}
              className={cn(
                "flex w-full items-center justify-between gap-2 rounded-[12px] px-2.5 py-1.5 text-left text-[12px] hover:bg-muted",
                id === value && "bg-muted",
              )}
            >
              <span className="truncate">{id}</span>
              {isSuggested ? (
                <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground">
                  {t("settings.models.suggested")}
                </span>
              ) : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
