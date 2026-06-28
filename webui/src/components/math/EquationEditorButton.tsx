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
  const fieldRef = useRef<HTMLElement & { value?: string }>(null);

  // Register the <math-field> custom element lazily when the dialog opens.
  useEffect(() => {
    if (open) void import("mathlive");
  }, [open]);

  const confirm = () => {
    const el = fieldRef.current;
    const tex = (el?.value ?? "").trim();
    if (tex) onInsert(`$${tex}$`);
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
          {createElement("math-field", {
            ref: fieldRef,
            "aria-label": t("composer.equationEditor"),
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
