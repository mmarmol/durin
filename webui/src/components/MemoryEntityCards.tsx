import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Clock, FileText, MessageCircle } from "lucide-react";

import type { MemoryGraphNode } from "@/lib/api";
import {
  browseEntities,
  colorForType,
  type EntitySortKey,
} from "@/lib/memory-graph-style";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface EntityBrowseViewProps {
  nodes: MemoryGraphNode[];
  hiddenTypes: Set<string>;
  query: string;
  sortKey: EntitySortKey;
  onSelect: (node: MemoryGraphNode) => void;
}

/** Cards presentation of the Entities tab: the reading-oriented inventory.
 *  Same node set and filters as the graph canvas; each card opens the same
 *  detail panel. */
export function MemoryEntityCards({
  nodes,
  hiddenTypes,
  query,
  sortKey,
  onSelect,
}: EntityBrowseViewProps) {
  const { t } = useTranslation();
  const entities = useMemo(
    () => browseEntities(nodes, { hiddenTypes, query, sortKey }),
    [nodes, hiddenTypes, query, sortKey],
  );

  if (entities.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        {t("memoryGraph.noEntitiesMatch")}
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-3">
      <div className="grid grid-cols-[repeat(auto-fill,minmax(190px,1fr))] gap-2.5">
        {entities.map((n) => (
          <button
            key={n.id}
            type="button"
            onClick={() => onSelect(n)}
            className={cn(
              "flex flex-col rounded-xl border bg-card px-3 py-2.5 text-left transition-colors hover:bg-muted/60",
              n.phantom ? "border-dashed border-border" : "border-border/50",
            )}
          >
            <div className="mb-1.5 flex items-center gap-1.5">
              <span
                className={cn(
                  "h-2 w-2 shrink-0 rounded-full",
                  n.phantom && "border border-dashed border-muted-foreground bg-transparent",
                )}
                style={n.phantom ? undefined : { background: colorForType(n.type) }}
              />
              <span className="truncate text-[10px] uppercase tracking-wide text-muted-foreground">
                {n.type}
                {n.phantom ? " · phantom" : ""}
              </span>
            </div>
            <div className="truncate text-[13px] font-medium">{n.name}</div>
            {n.summary ? (
              <p className="mt-1 line-clamp-2 text-[11.5px] leading-snug text-muted-foreground">
                {n.summary}
              </p>
            ) : null}
            <div className="mt-auto flex items-center gap-2.5 pt-2 text-[10.5px] text-muted-foreground">
              <span className="flex items-center gap-0.5">
                <MessageCircle className="h-3 w-3" aria-hidden /> {n.weight}
              </span>
              {n.updated_at ? (
                <span className="flex items-center gap-0.5">
                  <Clock className="h-3 w-3" aria-hidden />{" "}
                  {relativeTime(n.updated_at)}
                </span>
              ) : null}
              {(n.sources ?? 0) > 0 ? (
                <span className="flex items-center gap-0.5">
                  <FileText className="h-3 w-3" aria-hidden /> {n.sources}
                </span>
              ) : null}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
