import { useTranslation } from "react-i18next";
import { Settings, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { WorkItemCard } from "@/components/work/WorkItemCard";
import { useConcurrencySnapshot } from "@/hooks/useConcurrencySnapshot";
import type { ConcurrencyWorkItem, WorkItem } from "@/lib/types";

// Snapshot work items are compact (kind/id/label/status only) — no node tree
// or task text, so WorkItemCard renders them via its plain-label branch.
// `WorkItem.kind` has no "turn" variant; a turn maps to "workflow" so the
// card's header takes the plain-label path (task is left unset).
function toWorkItem(w: ConcurrencyWorkItem, label: string): WorkItem {
  return {
    kind: w.kind === "subagent" ? "subagent" : "workflow",
    id: w.id,
    label: w.label || label,
    status: "running",
    startedAt: 0,
    endedAt: null,
  };
}

/**
 * Global, gateway-wide Work panel — shows every running turn and sub-agent
 * across all sessions. Fed entirely by useConcurrencySnapshot()'s `work`
 * list (in-memory, pushed by the gateway); no polling, no per-session
 * subscriptions, no disk reads.
 */
export function GlobalWorkPanel({
  open,
  onClose,
  onOpenSettings,
}: {
  open: boolean;
  onClose: () => void;
  onOpenSettings: () => void;
}): JSX.Element {
  const { t } = useTranslation();
  const snap = useConcurrencySnapshot();
  if (!open) return <></>;

  const items = (snap?.work ?? []).map((w) =>
    toWorkItem(w, w.kind === "subagent" ? t("concurrency.panel.subagent") : t("concurrency.panel.turn")),
  );

  return (
    <aside
      className="flex w-72 shrink-0 flex-col border-l border-border/60 bg-background"
      aria-label={t("concurrency.panel.title")}
    >
      <div className="flex items-center justify-between border-b border-border/60 px-3 py-2.5">
        <span className="text-[13px] font-semibold text-foreground">
          {t("concurrency.panel.title")}
        </span>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("settings.nav.concurrency")}
            onClick={onOpenSettings}
            className="h-6 w-6 text-muted-foreground hover:text-foreground"
          >
            <Settings className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("concurrency.panel.close")}
            onClick={onClose}
            className="h-6 w-6 text-muted-foreground hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-3">
        {items.length === 0 ? (
          <p className="text-[12.5px] text-muted-foreground">{t("concurrency.panel.empty")}</p>
        ) : (
          items.map((item) => <WorkItemCard key={item.id} item={item} />)
        )}
      </div>
    </aside>
  );
}
