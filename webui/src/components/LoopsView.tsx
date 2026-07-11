import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ActivityView } from "@/components/loops/ActivityView";
import { DefinitionsView } from "@/components/loops/DefinitionsView";
import { LoopForm } from "@/components/loops/LoopForm";
import type { LoopDef } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

type LoopsPane = "activity" | "definitions";

export function LoopsView() {
  const { t } = useTranslation();
  const { token } = useClient();
  const [pane, setPane] = useState<LoopsPane>("activity");
  // undefined = editor panel closed; null = creating a new loop; a LoopDef =
  // editing that loop.
  const [editing, setEditing] = useState<LoopDef | null | undefined>(undefined);

  if (editing !== undefined) {
    return (
      <div className="flex h-full w-full flex-col">
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <button
            type="button"
            onClick={() => setEditing(undefined)}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            {t("loops.definitions.back")}
          </button>
          <span className="text-xs font-medium text-foreground/80">
            {editing ? t("loops.definitions.editTitle", { name: editing.name }) : t("loops.definitions.newTitle")}
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <LoopForm
            token={token}
            editLoop={editing}
            onDone={() => setEditing(undefined)}
            onCancel={() => setEditing(undefined)}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full w-full flex-col">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <div
          className="flex h-7 rounded-full bg-muted p-0.5"
          role="group"
          aria-label={t("loops.title")}
        >
          {(["activity", "definitions"] as const).map((opt) => (
            <button
              key={opt}
              type="button"
              onClick={() => setPane(opt)}
              aria-pressed={pane === opt}
              className={cn(
                "rounded-full px-3 text-[12.5px] font-medium transition-colors",
                pane === opt
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t(`loops.tab.${opt}`)}
            </button>
          ))}
        </div>
      </div>
      <div className={cn("flex min-h-0 flex-1", pane !== "activity" && "hidden")}>
        <ActivityView />
      </div>
      <div className={cn("flex min-h-0 flex-1", pane !== "definitions" && "hidden")}>
        <DefinitionsView onEdit={setEditing} />
      </div>
    </div>
  );
}
