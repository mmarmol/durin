import { useEffect, useRef, useState } from "react";
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type { ModeInfo } from "@/lib/api";

interface ModePickerProps {
  activeMode: string;
  modes: ModeInfo[];
  onSelect: (mode: string) => void;
  disabled?: boolean;
}

/** Composer pill that switches the agent's execution mode. Mode-agnostic: it
 *  renders whatever the backend registers (built-ins plus custom modes) by
 *  name, with one fixed glyph — never an icon hardcoded per mode name. */
export function ModePicker({
  activeMode,
  modes,
  onSelect,
  disabled,
}: ModePickerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

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

  if (modes.length === 0) return null;

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
        title={t("thread.composer.mode.title")}
        aria-label={t("thread.composer.mode.title")}
      >
        <SlidersHorizontal className="h-3 w-3" aria-hidden />
        <span className="max-w-[6rem] truncate">{activeMode}</span>
        <ChevronDown className="h-2.5 w-2.5 opacity-60" aria-hidden />
      </button>
      {open ? (
        <div
          role="listbox"
          aria-label={t("thread.composer.mode.title")}
          className={cn(
            "absolute bottom-full left-0 z-50 mb-2 w-[244px]",
            "rounded-xl border border-border/70 bg-popover shadow-xl",
            "animate-in fade-in-0 zoom-in-95 slide-in-from-bottom-2 duration-200",
          )}
        >
          <div className="py-1">
            {modes.map((mode) => {
              const selected = mode.name === activeMode;
              return (
                <button
                  key={mode.name}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => handleSelect(mode.name)}
                  className={cn(
                    "flex w-full items-start gap-2.5 px-3 py-2 text-left transition-colors",
                    selected ? "bg-accent/55" : "hover:bg-muted/60",
                  )}
                >
                  <span
                    aria-hidden
                    className={cn(
                      "mt-0.5 grid h-3.5 w-3.5 flex-none place-items-center rounded-full border",
                      selected ? "border-primary" : "border-border",
                    )}
                  >
                    {selected ? (
                      <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                    ) : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-[12px] font-medium text-foreground">
                      {mode.name}
                    </span>
                    {mode.description ? (
                      <span className="mt-0.5 block text-[11px] leading-snug text-muted-foreground">
                        {mode.description}
                      </span>
                    ) : null}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
