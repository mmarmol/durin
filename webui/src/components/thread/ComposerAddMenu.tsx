import { useEffect, useRef, useState } from "react";
import { Paperclip, Plus, Sigma, Slash } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

interface ComposerAddMenuProps {
  /** Open the native file picker (images + audio). */
  onAttach: () => void;
  /** Seed the input with "/" to open the slash-command palette. */
  onSlash: () => void;
  /** Open the equation editor. Omitted → the formula row is hidden. */
  onEquation?: () => void;
  /** Disables the whole trigger (composer disabled). */
  disabled?: boolean;
  /** Disables only the attach row (e.g. the image limit is reached). */
  attachDisabled?: boolean;
  variant?: "hero" | "thread";
}

/** The composer's "+" affordance: a single entry point for adding content
 *  (file/photo upload, slash commands) so those live behind one button instead
 *  of crowding the toolbar. Mirrors the add-menu pattern in Gemini/Claude. */
export function ComposerAddMenu({
  onAttach,
  onSlash,
  onEquation,
  disabled,
  attachDisabled,
  variant = "thread",
}: ComposerAddMenuProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const isHero = variant === "hero";

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

  const choose = (fn: () => void) => {
    setOpen(false);
    fn();
  };

  return (
    <div ref={containerRef} className="relative inline-flex">
      <button
        type="button"
        disabled={disabled}
        aria-label={t("thread.composer.add.title")}
        title={t("thread.composer.add.title")}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex items-center justify-center rounded-full text-muted-foreground hover:text-foreground",
          "border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
          "disabled:opacity-50",
          isHero ? "h-9 w-9" : "h-7.5 w-7.5",
        )}
      >
        <Plus className={cn(isHero ? "h-5 w-5" : "h-4 w-4")} />
      </button>
      {open ? (
        <div
          role="menu"
          className={cn(
            "absolute bottom-full left-0 z-50 mb-2 w-[210px]",
            "rounded-xl border border-border/70 bg-popover p-1 shadow-xl",
            "animate-in fade-in-0 zoom-in-95 slide-in-from-bottom-2 duration-200",
          )}
        >
          <button
            type="button"
            role="menuitem"
            disabled={attachDisabled}
            onClick={() => choose(onAttach)}
            className={cn(
              "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px]",
              "text-foreground/85 transition-colors hover:bg-muted/60 disabled:opacity-50 disabled:hover:bg-transparent",
            )}
          >
            <Paperclip className="h-4 w-4 flex-none text-muted-foreground" aria-hidden />
            {t("thread.composer.add.attach")}
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => choose(onSlash)}
            className={cn(
              "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px]",
              "text-foreground/85 transition-colors hover:bg-muted/60",
            )}
          >
            <Slash className="h-4 w-4 flex-none text-muted-foreground" aria-hidden />
            {t("thread.composer.add.slash")}
          </button>
          {onEquation ? (
            <button
              type="button"
              role="menuitem"
              onClick={() => choose(onEquation)}
              className={cn(
                "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px]",
                "text-foreground/85 transition-colors hover:bg-muted/60",
              )}
            >
              <Sigma className="h-4 w-4 flex-none text-muted-foreground" aria-hidden />
              {t("thread.composer.add.equation")}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
