import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp } from "lucide-react";
import { useTranslation } from "react-i18next";

import { MarkdownText } from "@/components/MarkdownText";
import { EquationEditorButton } from "@/components/math/EquationEditorButton";
import { insertAtCursor } from "@/components/math/insert-at-cursor";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ComposerProps {
  onSend: (content: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** Visually collapse the outer padding when embedded inside a welcome screen. */
  compact?: boolean;
}

/**
 * Rounded, shadowed composer with an embedded send button — modeled after the
 * agent-chat-ui input: a single surface that looks like one interactive unit
 * rather than a textarea + button pair.
 */
export function Composer({
  onSend,
  disabled,
  placeholder,
  compact = false,
}: ComposerProps) {
  const { t } = useTranslation();
  const resolvedPlaceholder = placeholder ?? t("composer.placeholder");
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Autofocus on mount — coming back to a chat, switching sessions, or
  // opening the welcome screen should always land the caret in the box.
  useEffect(() => {
    if (disabled) return;
    const el = textareaRef.current;
    if (!el) return;
    // Defer so layout settles first (important during enter animations).
    const id = requestAnimationFrame(() => el.focus());
    return () => cancelAnimationFrame(id);
  }, [disabled]);

  const submit = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.style.height = "auto";
        el.focus();
      }
    });
  }, [disabled, onSend, value]);

  const onKeyDown: React.KeyboardEventHandler<HTMLTextAreaElement> = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const onInput: React.FormEventHandler<HTMLTextAreaElement> = (e) => {
    const el = e.currentTarget;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
  };

  const onInsertEquation = (latex: string) => {
    const el = textareaRef.current;
    const start = el?.selectionStart ?? value.length;
    const end = el?.selectionEnd ?? value.length;
    const { next, caret } = insertAtCursor(value, start, end, latex);
    setValue(next);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(caret, caret);
    });
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className={cn(
        "w-full",
        compact ? "px-0" : "bg-background/95 px-4 pb-4 pt-2 backdrop-blur",
      )}
    >
      <div
        className={cn(
          "relative mx-auto flex w-full max-w-[64rem] flex-col overflow-hidden rounded-3xl",
          "border bg-muted/60 shadow-sm transition-all duration-200",
          "focus-within:bg-muted focus-within:shadow-md focus-within:ring-1 focus-within:ring-foreground/10",
          disabled && "opacity-60",
        )}
      >
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onInput={onInput}
          onKeyDown={onKeyDown}
          rows={1}
          placeholder={resolvedPlaceholder}
          disabled={disabled}
          aria-label={t("composer.inputAria")}
          className={cn(
            "min-h-[56px] w-full resize-none bg-transparent px-5 pt-4 pb-2 text-sm",
            "placeholder:text-muted-foreground",
            "focus:outline-none focus-visible:outline-none",
            "disabled:cursor-not-allowed",
          )}
        />
        {value.includes("$") && (
          <div className="border-t border-border/40 px-3 py-2 text-sm">
            <MarkdownText>{value}</MarkdownText>
          </div>
        )}
        <div className="flex items-center justify-between gap-2 px-3 pb-2">
          <span className="hidden select-none text-[11px] text-muted-foreground/70 sm:inline">
            {t("composer.hint")}
          </span>
          <span className="sm:hidden" aria-hidden />
          <div className="flex items-center gap-1">
            <EquationEditorButton onInsert={onInsertEquation} />
            <Button
              type="submit"
              size="icon"
              disabled={disabled || !value.trim()}
              aria-label={t("composer.sendAria")}
              className={cn(
                "h-9 w-9 rounded-full shadow-sm transition-transform",
                value.trim() && !disabled && "hover:scale-[1.03] active:scale-95",
              )}
            >
              <ArrowUp className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>
    </form>
  );
}
