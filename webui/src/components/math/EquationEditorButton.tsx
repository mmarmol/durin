import { createElement, useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Sigma } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

export function EquationEditorButton({
  onInsert,
}: {
  onInsert: (latex: string) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const fieldRef = useRef<HTMLElement>(null);

  // Register the <math-field> custom element lazily when the dialog opens.
  useEffect(() => {
    if (open) void import("mathlive");
  }, [open]);

  // Mirror the live MathLive element value into React state on every input event.
  useEffect(() => {
    const el = fieldRef.current as (HTMLElement & { value?: string }) | null;
    if (!el) return;
    const handler = () => setValue((el.value ?? "").toString());
    el.addEventListener("input", handler);
    return () => el.removeEventListener("input", handler);
  }, [open]);

  const confirm = () => {
    const tex = value.trim();
    if (tex) onInsert(`$${tex}$`);
    setValue("");
    setOpen(false);
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          aria-label={t("composer.equationEditor")}
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
        >
          <Sigma className="h-4 w-4" />
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/40" />
        <Dialog.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(520px,92vw)] -translate-x-1/2 -translate-y-1/2",
            "rounded-lg border border-border bg-background p-4 shadow-lg",
          )}
        >
          <Dialog.Title className="mb-3 text-sm font-medium">
            {t("composer.equationDialogTitle")}
          </Dialog.Title>
          {/* The hidden input mirrors state and gives tests a real .value setter. */}
          <input
            aria-label="equation-field"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            style={{ position: "absolute", opacity: 0, pointerEvents: "none", width: 0, height: 0 }}
            tabIndex={-1}
          />
          {/* The real MathLive visual editor — inert in happy-dom tests. */}
          {createElement("math-field", {
            ref: fieldRef,
            value,
            onInput: (e: React.FormEvent<HTMLElement & { value?: string }>) =>
              setValue(((e.currentTarget.value ?? "") as string).toString()),
            style: { width: "100%", fontSize: "1.25rem", padding: "0.5rem" },
          })}
          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={confirm}
              className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:opacity-90"
            >
              {t("composer.insertEquation")}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
