import { createElement, useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Sigma } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

export function EquationEditorButton({
  onInsert,
  open: openProp,
  onOpenChange,
  hideTrigger = false,
}: {
  onInsert: (latex: string) => void;
  /** Controlled open state. Omit for the standalone (self-managed) button. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Render only the dialog, no trigger — for opening from another control
   *  (e.g. the composer's "+" menu). */
  hideTrigger?: boolean;
}) {
  const { t } = useTranslation();
  const [openState, setOpenState] = useState(false);
  const open = openProp ?? openState;
  const setOpen = (next: boolean) => {
    onOpenChange?.(next);
    if (openProp === undefined) setOpenState(next);
  };
  const fieldRef = useRef<HTMLElement & { value?: string }>(null);

  // Register the <math-field> custom element lazily when the dialog opens, then
  // focus it so the editor opens ready to type. Without this the field looks
  // empty and inert until the user clicks, as if nothing had loaded.
  useEffect(() => {
    if (!open) return;
    let active = true;
    void import("mathlive").then(() => {
      if (active) fieldRef.current?.focus();
    });
    return () => {
      active = false;
    };
  }, [open]);

  const confirm = () => {
    const el = fieldRef.current;
    const tex = (el?.value ?? "").trim();
    if (tex) onInsert(`$${tex}$`);
    setOpen(false);
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      {hideTrigger ? null : (
        <Dialog.Trigger asChild>
          <button
            type="button"
            aria-label={t("composer.equationEditor")}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
          >
            <Sigma className="h-4 w-4" />
          </button>
        </Dialog.Trigger>
      )}
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/40" />
        <Dialog.Content
          onOpenAutoFocus={(e) => e.preventDefault()}
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
