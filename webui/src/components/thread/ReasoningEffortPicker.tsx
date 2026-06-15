import { useState, useRef, useEffect } from "react";
import { ChevronDown, Zap } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

const EFFORT_LEVELS = [
  { value: "", labelKey: "thread.composer.effort.default" },
  { value: "none", labelKey: "thread.composer.effort.off" },
  { value: "high", labelKey: "thread.composer.effort.high" },
  { value: "max", labelKey: "thread.composer.effort.max" },
] as const;

interface ReasoningEffortPickerProps {
  activeEffort: string | null;
  onSelect: (effort: string) => void;
  disabled?: boolean;
}

export function ReasoningEffortPicker({
  activeEffort,
  onSelect,
  disabled,
}: ReasoningEffortPickerProps) {
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

  const currentLabel = (() => {
    const match = EFFORT_LEVELS.find((l) => l.value === (activeEffort ?? ""));
    return match ? t(match.labelKey) : t("thread.composer.effort.default");
  })();

  const handleSelect = (value: string) => {
    onSelect(value);
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
        title={t("thread.composer.effort.title")}
      >
        <Zap className="h-3 w-3" aria-hidden />
        <span className="max-w-[5rem] truncate">{currentLabel}</span>
        <ChevronDown className="h-2.5 w-2.5 opacity-60" aria-hidden />
      </button>
      {open ? (
        <div
          className={cn(
            "absolute bottom-full left-0 z-50 mb-2 w-[160px]",
            "rounded-xl border border-border/70 bg-popover shadow-xl",
            "animate-in fade-in-0 zoom-in-95 slide-in-from-bottom-2 duration-200",
          )}
        >
          <div className="py-1">
            {EFFORT_LEVELS.map((level) => (
              <button
                key={level.value}
                type="button"
                onClick={() => handleSelect(level.value)}
                className={cn(
                  "flex w-full items-center justify-between px-3 py-1.5 text-left text-[12px] transition-colors",
                  (level.value === (activeEffort ?? ""))
                    ? "bg-accent/60 font-medium text-accent-foreground"
                    : "text-foreground/85 hover:bg-muted/60",
                )}
              >
                <span>{t(level.labelKey)}</span>
                {level.value === (activeEffort ?? "") ? (
                  <span className="text-[10px] text-emerald-500">●</span>
                ) : null}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
