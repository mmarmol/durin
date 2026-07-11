import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ActivityView } from "@/components/loops/ActivityView";
import { DefinitionsView } from "@/components/loops/DefinitionsView";
import type { LoopDef } from "@/lib/api";
import { cn } from "@/lib/utils";

type LoopsPane = "activity" | "definitions";

export function LoopsView() {
  const { t } = useTranslation();
  const [pane, setPane] = useState<LoopsPane>("activity");
  // undefined = editor panel closed; null = creating a new loop; a LoopDef =
  // editing that loop. The actual edit form lands in a later task — this just
  // renders a placeholder so DefinitionsView's "New loop"/"Edit" affordances
  // have somewhere to go, wired for that form to slot in without touching
  // this state shape.
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
        </div>
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          {editing
            ? t("loops.definitions.editPlaceholder", { name: editing.name })
            : t("loops.definitions.newPlaceholder")}
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
