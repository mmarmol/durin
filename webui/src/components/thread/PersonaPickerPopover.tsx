import { useEffect, useRef, useState } from "react";
import { ChevronDown, Drama } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { listPersonas, type PersonaItem } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

interface PersonaPickerPopoverProps {
  activePersona: string | null;
  onSelect: (name: string) => void;
  disabled?: boolean;
}

/** Composer pill that switches the active persona. Shows the current persona
 *  name (or a generic label) and opens a dropdown listing all personas from
 *  the backend. Lazy-loads on first open; mirrors ModePicker's structure. */
export function PersonaPickerPopover({
  activePersona,
  onSelect,
  disabled,
}: PersonaPickerPopoverProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [open, setOpen] = useState(false);
  const [personas, setPersonas] = useState<PersonaItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Lazy-load personas on first open.
  useEffect(() => {
    if (!open || loaded) return;
    listPersonas(token)
      .then(({ personas: items }) => {
        setPersonas(items);
        setLoaded(true);
      })
      .catch(() => {});
  }, [open, loaded, token]);

  // Outside-click close.
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

  const handleSelect = (name: string) => {
    onSelect(name);
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
        title={t("thread.composer.persona.title")}
        aria-label={t("thread.composer.persona.title")}
      >
        <Drama className="h-3 w-3" aria-hidden />
        <span className="max-w-[6rem] truncate">
          {activePersona ?? t("thread.composer.persona.title")}
        </span>
        <ChevronDown className="h-2.5 w-2.5 opacity-60" aria-hidden />
      </button>
      {open ? (
        <div
          role="listbox"
          aria-label={t("thread.composer.persona.title")}
          className={cn(
            "absolute bottom-full left-0 z-50 mb-2 w-[200px]",
            "rounded-xl border border-border/70 bg-popover shadow-xl",
            "animate-in fade-in-0 zoom-in-95 slide-in-from-bottom-2 duration-200",
          )}
        >
          <div className="py-1">
            {personas.length === 0 ? (
              <div className="px-3 py-4 text-center text-[12px] text-muted-foreground">
                {t("thread.composer.persona.empty")}
              </div>
            ) : null}
            {personas.map((persona) => {
              const selected = persona.name === activePersona;
              return (
                <button
                  key={persona.name}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => handleSelect(persona.name)}
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
                      <span className="ml-2 flex-none text-[10px] text-emerald-500" aria-hidden>
                        ●
                      </span>
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
        </div>
      ) : null}
    </div>
  );
}
