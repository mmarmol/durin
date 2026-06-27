import { X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import type { WorkItem } from "@/lib/types";
import { WorkItemCard } from "./WorkItemCard";

/**
 * Collapsible right-side work panel — presentational only. The Shell (App.tsx)
 * owns the single useWorkState() subscription and passes active/finished down.
 *
 * When open=false the panel renders nothing so the chat takes full width.
 * When open=true it docks as a flex sibling after <main> in the outer row.
 */
export function WorkPanel({
  active,
  finished,
  open,
  onClose,
}: {
  active: WorkItem[];
  finished: WorkItem[];
  open: boolean;
  onClose: () => void;
}): JSX.Element {
  const { t } = useTranslation();

  if (!open) return <></>;

  return (
    <aside
      className="flex w-72 shrink-0 flex-col border-l border-border/60 bg-background"
      aria-label={t("work.panelTitle")}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border/60 px-3 py-2.5">
        <span className="text-[13px] font-semibold text-foreground">
          {t("work.panelTitle")}
        </span>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("work.close")}
          onClick={onClose}
          className="h-6 w-6 text-muted-foreground hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
        {/* Active section */}
        <section>
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            {t("work.inProgress")}
          </p>
          {active.length === 0 ? (
            <p className="text-[12.5px] text-muted-foreground">
              {t("work.empty")}
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {active.map((item) => (
                <WorkItemCard key={item.id} item={item} />
              ))}
            </div>
          )}
        </section>

        {/* Finished section — only shown when there are finished items */}
        {finished.length > 0 && (
          <section>
            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {t("work.finished", { count: finished.length })}
            </p>
            <div className="flex flex-col gap-2">
              {finished.map((item) => (
                <WorkItemCard key={item.id} item={item} />
              ))}
            </div>
          </section>
        )}
      </div>
    </aside>
  );
}
