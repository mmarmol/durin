import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronUp } from "lucide-react";

import type { MemoryGraphNode } from "@/lib/api";
import {
  browseEntities,
  colorForType,
  type EntitySortKey,
} from "@/lib/memory-graph-style";
import type { EntityBrowseViewProps } from "@/components/MemoryEntityCards";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type ColumnKey = "name" | "type" | "mentions" | "updated" | "sources";

interface ColumnSort {
  key: ColumnKey;
  dir: "asc" | "desc";
}

// Map the shared toolbar sort to an initial column sort so switching from
// cards to table keeps the ordering the user chose.
function initialSort(sortKey: EntitySortKey): ColumnSort {
  if (sortKey === "name") return { key: "name", dir: "asc" };
  if (sortKey === "mentions") return { key: "mentions", dir: "desc" };
  return { key: "updated", dir: "desc" };
}

function compareBy(key: ColumnKey, a: MemoryGraphNode, b: MemoryGraphNode): number {
  switch (key) {
    case "name":
      return a.name.localeCompare(b.name);
    case "type":
      return a.type.localeCompare(b.type) || a.name.localeCompare(b.name);
    case "mentions":
      return a.weight - b.weight;
    case "updated":
      return (a.updated_at ?? "").localeCompare(b.updated_at ?? "");
    case "sources":
      return (a.sources ?? 0) - (b.sources ?? 0);
  }
}

/** Table presentation of the Entities tab: the audit-oriented inventory.
 *  Column headers re-sort in place; rows open the same detail panel as the
 *  graph and cards views. */
export function MemoryEntityTable({
  nodes,
  hiddenTypes,
  query,
  sortKey,
  onSelect,
}: EntityBrowseViewProps) {
  const { t } = useTranslation();
  const [colSort, setColSort] = useState<ColumnSort>(() => initialSort(sortKey));

  const entities = useMemo(() => {
    const base = browseEntities(nodes, { hiddenTypes, query, sortKey });
    const sorted = [...base].sort((a, b) => compareBy(colSort.key, a, b));
    if (colSort.dir === "desc") sorted.reverse();
    return sorted;
  }, [nodes, hiddenTypes, query, sortKey, colSort]);

  function toggleSort(key: ColumnKey) {
    setColSort((cur) =>
      cur.key === key
        ? { key, dir: cur.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "name" || key === "type" ? "asc" : "desc" },
    );
  }

  if (entities.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        {t("memoryGraph.noEntitiesMatch")}
      </div>
    );
  }

  const columns: { key: ColumnKey; label: string; className?: string }[] = [
    { key: "name", label: t("memoryGraph.colEntity") },
    { key: "type", label: t("memoryGraph.colType") },
    { key: "mentions", label: t("memoryGraph.colMentions"), className: "text-right" },
    { key: "updated", label: t("memoryGraph.colUpdated") },
    { key: "sources", label: t("memoryGraph.colSources"), className: "text-right" },
  ];

  return (
    <div className="h-full overflow-y-auto p-3">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="text-left text-muted-foreground">
            {columns.map((c) => (
              <th
                key={c.key}
                role="columnheader"
                aria-sort={
                  colSort.key === c.key
                    ? colSort.dir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className={cn("border-b border-border/60 p-0", c.className)}
              >
                <button
                  type="button"
                  onClick={() => toggleSort(c.key)}
                  className={cn(
                    "flex w-full items-center gap-0.5 px-2 py-1.5 font-medium hover:bg-muted/60",
                    c.className === "text-right" && "justify-end",
                  )}
                >
                  {c.label}
                  {colSort.key === c.key ? (
                    colSort.dir === "asc" ? (
                      <ChevronUp className="h-3 w-3" aria-hidden />
                    ) : (
                      <ChevronDown className="h-3 w-3" aria-hidden />
                    )
                  ) : null}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {entities.map((n) => (
            <tr
              key={n.id}
              onClick={() => onSelect(n)}
              className="cursor-pointer border-b border-border/30 hover:bg-muted/60"
            >
              <td className="max-w-0 truncate px-2 py-1.5">
                <span
                  className={cn(
                    "mr-1.5 inline-block h-2 w-2 rounded-full align-middle",
                    n.phantom &&
                      "border border-dashed border-muted-foreground bg-transparent",
                  )}
                  style={
                    n.phantom ? undefined : { background: colorForType(n.type) }
                  }
                />
                <span className="align-middle font-medium">{n.name}</span>
                {n.phantom ? (
                  <span className="ml-1 align-middle text-[10px] text-muted-foreground">
                    · phantom
                  </span>
                ) : null}
              </td>
              <td className="px-2 py-1.5 text-muted-foreground">{n.type}</td>
              <td className="px-2 py-1.5 text-right tabular-nums">{n.weight}</td>
              <td className="px-2 py-1.5 text-muted-foreground">
                {n.updated_at ? relativeTime(n.updated_at) : "—"}
              </td>
              <td className="px-2 py-1.5 text-right tabular-nums text-muted-foreground">
                {n.sources ?? 0}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
