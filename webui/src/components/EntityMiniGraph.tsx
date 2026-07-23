import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchMemorySubgraph, type MemoryGraphNode } from "@/lib/api";
import { colorForType, shortLabel } from "@/lib/memory-graph-style";

interface EntityMiniGraphProps {
  token: string | null;
  entityRef: string;
  entityName: string;
  onNavigate: (ref: string, name: string) => void;
  onViewInGraph: () => void;
}

// A compact preview, not the full canvas — a tighter cap than any limit the
// interactive graph applies elsewhere.
const MAX_NEIGHBORS = 12;
const VIEW_SIZE = 200;
const CENTER = VIEW_SIZE / 2;
const RING_RADIUS = 66;

/** Ego-graph preview for the entity detail panel's Info tab ("Related"): the
 *  selected entity's direct (1-hop) neighbours laid out on a ring, so the
 *  user can jump to a related entity — or open the full interactive graph —
 *  without leaving the list/cards presentation the panel was opened from. */
export function EntityMiniGraph({
  token,
  entityRef,
  entityName,
  onNavigate,
  onViewInGraph,
}: EntityMiniGraphProps) {
  const { t } = useTranslation();
  const [neighbors, setNeighbors] = useState<MemoryGraphNode[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!token) {
      // No client session to fetch with — hide silently rather than claim
      // "no relations" for an entity we never actually checked.
      setLoading(false);
      setFailed(true);
      return;
    }
    setLoading(true);
    setFailed(false);
    setNeighbors(null);
    void fetchMemorySubgraph(token, entityRef, { hops: 1 })
      .then((payload) => {
        if (cancelled) return;
        const top = payload.nodes
          .filter((n) => n.id !== entityRef && n.type !== "session" && !n.phantom)
          .sort((a, b) => b.weight - a.weight)
          .slice(0, MAX_NEIGHBORS);
        setNeighbors(top);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, entityRef]);

  // Ring layout: neighbours spaced evenly around the centre, starting at the
  // top (-90°) so a single neighbour lands straight above rather than to the
  // side.
  const points = useMemo(() => {
    const list = neighbors ?? [];
    return list.map((node, i) => {
      const angle = (i / Math.max(1, list.length)) * Math.PI * 2 - Math.PI / 2;
      return {
        node,
        x: CENTER + Math.cos(angle) * RING_RADIUS,
        y: CENTER + Math.sin(angle) * RING_RADIUS,
      };
    });
  }, [neighbors]);

  // A fetch failure hides the whole section silently — the panel's main
  // content (Info tab fields, other tabs) is unaffected either way.
  if (failed) return null;

  return (
    <div className="mb-3 border-b border-border/30 pb-3">
      <div className="mb-1.5 flex items-center gap-1.5">
        <span className="text-[11px] font-semibold">{t("memoryGraph.relatedTitle")}</span>
        <span className="text-[10px] text-muted-foreground">{t("memoryGraph.relatedOneHop")}</span>
        <button
          type="button"
          onClick={onViewInGraph}
          className="ml-auto rounded border border-border/40 px-1.5 py-0.5 text-[10px] text-primary hover:bg-muted"
        >
          {t("memoryGraph.viewInGraph")}
        </button>
      </div>
      {loading ? (
        <div className="h-28 w-full animate-pulse rounded-md bg-muted/50" />
      ) : (neighbors?.length ?? 0) === 0 ? (
        <p className="text-[11px] text-muted-foreground">{t("memoryGraph.relatedEmpty")}</p>
      ) : (
        <svg
          viewBox={`0 0 ${VIEW_SIZE} ${VIEW_SIZE}`}
          className="mx-auto block h-44 w-44"
          role="img"
          aria-label={`${t("memoryGraph.relatedTitle")}: ${entityName}`}
        >
          {points.map(({ node, x, y }) => (
            <line
              key={`edge-${node.id}`}
              x1={CENTER}
              y1={CENTER}
              x2={x}
              y2={y}
              className="stroke-current text-border"
              strokeOpacity={0.6}
              strokeWidth={1}
            />
          ))}
          {/* Center dot stands for the panel's own entity — colored by the
              type its ref prefix names, the same `<type>:<slug>` convention
              handleOpenEntity/isolateNode already parse elsewhere. Its name
              isn't repeated here: the panel header right above already
              shows it. */}
          <circle
            cx={CENTER}
            cy={CENTER}
            r={8}
            fill={colorForType(entityRef.split(":")[0] || "unknown")}
          />
          {points.map(({ node, x, y }) => (
            <g
              key={node.id}
              role="button"
              tabIndex={0}
              aria-label={node.name}
              onClick={() => onNavigate(node.id, node.name)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onNavigate(node.id, node.name);
                }
              }}
              className="cursor-pointer outline-none"
            >
              <circle cx={x} cy={y} r={5} fill={colorForType(node.type)} />
              <text
                x={x}
                y={y + 13}
                textAnchor="middle"
                fontSize={10}
                className="fill-current text-muted-foreground"
              >
                {shortLabel(node.name, 12)}
              </text>
            </g>
          ))}
        </svg>
      )}
    </div>
  );
}
