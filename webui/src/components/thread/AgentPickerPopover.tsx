import { useEffect, useRef, useState } from "react";
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { listPersonas, type ModeInfo, type PersonaItem } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

interface AgentPickerPopoverProps {
  activeMode: string;
  modes: ModeInfo[];
  onModeSelect?: (mode: string) => void;
  activePersona: string | null;
  onPersonaSelect?: (name: string) => void;
  disabled?: boolean;
}

/** Composer pill combining agent mode + persona. One trigger showing
 *  "mode · persona"; the popover lists both sections. Personas lazy-load on
 *  first open, or eagerly on mount when no persona is active yet so the pill
 *  and listbox can fall back to the server's default persona. */
export function AgentPickerPopover({
  activeMode,
  modes,
  onModeSelect,
  activePersona,
  onPersonaSelect,
  disabled,
}: AgentPickerPopoverProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [open, setOpen] = useState(false);
  const [personas, setPersonas] = useState<PersonaItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [defaultPersona, setDefaultPersona] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const showModes = !!onModeSelect && modes.length > 0;
  const showPersonas = !!onPersonaSelect;

  useEffect(() => {
    if (loaded || !showPersonas || !(open || activePersona === null)) return;
    let cancelled = false;
    listPersonas(token)
      .then(({ personas: items, default: def }) => {
        if (cancelled) return;
        setPersonas(items);
        setDefaultPersona(def);
        setLoaded(true);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open, loaded, showPersonas, activePersona, token]);

  // The pill and the listbox both need a persona to show as "current" even
  // before the user ever picks one explicitly — fall back to the server's
  // default persona once it's known.
  const effectivePersona = activePersona ?? defaultPersona;

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  if (!showModes && !showPersonas) return null;

  const label = [
    showModes ? activeMode : null,
    showPersonas ? effectivePersona : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const pick = (fn: ((v: string) => void) | undefined, value: string) => {
    fn?.(value);
    setOpen(false);
  };

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex items-center gap-1 rounded-full border px-2 py-1",
          "border-foreground/10 bg-foreground/[0.035] text-[10.5px] font-medium text-foreground/80",
          "transition-colors hover:bg-foreground/[0.06] disabled:opacity-50",
        )}
        title={t("thread.composer.agent.title")}
        aria-label={t("thread.composer.agent.title")}
      >
        <SlidersHorizontal className="h-3 w-3" aria-hidden />
        <span className="max-w-[10rem] truncate">
          {label || t("thread.composer.agent.title")}
        </span>
        <ChevronDown className="h-2.5 w-2.5 opacity-60" aria-hidden />
      </button>
      {open ? (
        <div
          className={cn(
            "absolute bottom-full left-0 z-50 mb-2 w-[220px]",
            "rounded-xl border border-border/70 bg-popover shadow-xl",
            "animate-in fade-in-0 zoom-in-95 slide-in-from-bottom-2 duration-200",
          )}
        >
          {showModes ? (
            <div role="listbox" aria-label={t("thread.composer.mode.title")} className="py-1">
              <div className="px-3 pb-0.5 pt-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground/70">
                {t("thread.composer.mode.title")}
              </div>
              {modes.map((mode) => {
                const selected = mode.name === activeMode;
                return (
                  <button
                    key={mode.name}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    onClick={() => pick(onModeSelect, mode.name)}
                    className={cn(
                      "flex w-full items-center justify-between px-3 py-1.5 text-left text-[12px] transition-colors",
                      selected
                        ? "bg-accent/60 font-medium text-accent-foreground"
                        : "text-foreground/85 hover:bg-muted/60",
                    )}
                  >
                    <span className="truncate">{mode.name}</span>
                    {selected ? (
                      <span className="ml-2 text-[10px] text-emerald-500" aria-hidden>●</span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          ) : null}
          {showModes && showPersonas ? (
            <div className="mx-3 border-t border-border/60" />
          ) : null}
          {showPersonas ? (
            <div role="listbox" aria-label={t("thread.composer.persona.title")} className="py-1">
              <div className="px-3 pb-0.5 pt-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground/70">
                {t("thread.composer.persona.title")}
              </div>
              {loaded && personas.length === 0 ? (
                <div className="px-3 py-3 text-center text-[12px] text-muted-foreground">
                  {t("thread.composer.persona.empty")}
                </div>
              ) : null}
              {personas.map((persona) => {
                const selected = persona.name === effectivePersona;
                return (
                  <button
                    key={persona.name}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    onClick={() => pick(onPersonaSelect, persona.name)}
                    className={cn(
                      "flex w-full flex-col gap-0.5 px-3 py-1.5 text-left transition-colors",
                      selected
                        ? "bg-accent/60 font-medium text-accent-foreground"
                        : "text-foreground/85 hover:bg-muted/60",
                    )}
                  >
                    <span className="flex w-full items-center justify-between text-[12px]">
                      <span className="truncate">{persona.name}</span>
                      {selected ? (
                        <span className="ml-2 flex-none text-[10px] text-emerald-500" aria-hidden>●</span>
                      ) : null}
                    </span>
                    {persona.description ? (
                      <span className="truncate text-[10px] text-muted-foreground/60">
                        {persona.description}
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
