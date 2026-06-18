import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Input } from "@/components/ui/input";
import { fetchProviderModels, listModels, type ProviderModelEntry } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Option {
  id: string;
  caps?: string;
  suggested?: boolean;
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

function capsLine(e: ProviderModelEntry, t: (k: string) => string): string {
  const parts: string[] = [];
  const ctx = formatCtx(e.max_input_tokens);
  if (ctx) parts.push(ctx);
  if (e.supports_vision) parts.push(t("settings.providerModels.capVision"));
  if (e.supports_audio_input) parts.push(t("settings.providerModels.capAudio"));
  if (e.supports_reasoning) parts.push(t("settings.providerModels.capReasoning"));
  return parts.join(" · ");
}

function capabilityOk(e: ProviderModelEntry, capability: string): boolean {
  if (capability === "vision") return !!e.supports_vision;
  if (capability === "audio") return !!e.supports_audio_input;
  return true;
}

/** Searchable model field: type freely, or pick from the provider's catalog.
 *
 *  ``capability``: optional ``"vision" | "audio" | "text"``. When set, only
 *  models that support the requested modality are surfaced (the vision aux
 *  picker won't suggest text-only models).
 *
 *  When a concrete provider is selected the options come from the per-provider
 *  catalog and carry capability badges, so you can see what a model supports
 *  while choosing. With provider ``auto`` (or none) it falls back to the
 *  cross-provider model list (no per-model badges available there).
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
  const [options, setOptions] = useState<Option[]>([]);
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    const concrete = !!provider && provider !== "auto";
    if (concrete) {
      fetchProviderModels(token, provider)
        .then((entries) => {
          if (cancelled) return;
          setOptions(
            entries
              .filter((e) => capabilityOk(e, capability))
              .map((e) => ({ id: e.id, caps: capsLine(e, t) || undefined })),
          );
        })
        .catch(() => {
          if (!cancelled) setOptions([]);
        });
    } else {
      listModels(token, provider, capability)
        .then((catalog) => {
          if (cancelled) return;
          const sset = new Set(catalog.suggested);
          const merged = [
            ...catalog.suggested.map((id) => ({ id, suggested: true })),
            ...catalog.models.filter((m) => !sset.has(m)).map((id) => ({ id })),
          ];
          setOptions(merged);
        })
        .catch(() => {
          if (!cancelled) setOptions([]);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [token, provider, capability, t]);

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
    const out: Option[] = [];
    for (const o of options) {
      if (!query || o.id.toLowerCase().includes(query)) out.push(o);
      if (out.length >= 40) break;
    }
    return out;
  }, [options, query]);

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
          {filtered.map((o) => (
            <button
              key={o.id}
              type="button"
              onClick={() => {
                onChange(o.id);
                setOpen(false);
              }}
              className={cn(
                "flex w-full items-center justify-between gap-2 rounded-[12px] px-2.5 py-1.5 text-left text-[12px] hover:bg-muted",
                o.id === value && "bg-muted",
              )}
            >
              <span className="truncate">{o.id}</span>
              {o.caps ? (
                <span className="shrink-0 text-[10px] text-muted-foreground">{o.caps}</span>
              ) : o.suggested ? (
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
