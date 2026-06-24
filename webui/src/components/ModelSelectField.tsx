import { useEffect, useRef, useState } from "react";
import { ChevronDown, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { fetchModelPicker, type PickerEntry } from "@/lib/api";
import { ModelPickerPopover } from "@/components/thread/ModelPickerPopover";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

/** A trigger button + popover that lets the user pick a model ref.
 *  value="" means "use agent default". onChange receives the ref string
 *  or "" to reset to default. */
export function ModelSelectField({
  value,
  onChange,
}: {
  value: string;
  onChange: (ref: string) => void;
}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<PickerEntry[]>([]);
  const wrapperRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchModelPicker(token, []).then(setEntries).catch(() => {});
  }, [token]);

  const displayName = value
    ? (entries.find((e) => e.ref === value)?.name ?? value)
    : t("settings.cron.modelDefault");

  const handleSelect = (ref: string) => {
    onChange(ref);
    setOpen(false);
  };

  return (
    <div ref={wrapperRef} className="relative inline-flex items-center gap-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-background",
          "px-2.5 py-1.5 text-[13px] text-foreground",
          "focus:outline-none focus:ring-1 focus:ring-ring",
          "hover:bg-muted/60 transition-colors",
        )}
        aria-label={t("settings.cron.fieldModel")}
      >
        <span className="max-w-[200px] truncate">{displayName}</span>
        <ChevronDown className="h-3.5 w-3.5 text-muted-foreground flex-none" aria-hidden />
      </button>
      {value ? (
        <button
          type="button"
          onClick={() => onChange("")}
          className="rounded p-0.5 text-muted-foreground hover:text-foreground transition-colors"
          aria-label={t("settings.cron.modelDefault")}
          title={t("settings.cron.modelDefault")}
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      ) : null}
      <ModelPickerPopover
        open={open}
        onClose={() => setOpen(false)}
        onSelect={handleSelect}
        activeModel={value || null}
        anchorRef={wrapperRef}
      />
    </div>
  );
}
