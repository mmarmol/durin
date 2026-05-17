import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DeliberationResultData } from "@/lib/types";

const ROLE_COLORS: Record<string, string> = {
  pragmatico: "border-l-blue-400",
  explorador: "border-l-amber-400",
  critico: "border-l-rose-400",
};

const ROLE_LABELS: Record<string, string> = {
  pragmatico: "Pragmático",
  explorador: "Explorador",
  critico: "Crítico",
};

interface DeliberationPanelProps {
  data: DeliberationResultData;
}

export function DeliberationPanel({ data }: DeliberationPanelProps) {
  const [expanded, setExpanded] = useState(false);

  if (!data.winner) return null;

  return (
    <div className="flex flex-col gap-1.5 py-1">
      {/* Winner summary */}
      <div className="flex items-center gap-2 text-[11px]">
        <span className="font-medium text-foreground/80">
          {ROLE_LABELS[data.winner.role] ?? data.winner.role}
        </span>
        <span className="text-muted-foreground">
          score {(data.winner.score * 10).toFixed(1)}/10
        </span>
        {data.under_doubt && (
          <span className="rounded bg-amber-100 px-1 text-[10px] text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
            bajo duda
          </span>
        )}
        <span className="ml-auto text-muted-foreground/70">
          umbral {(data.threshold * 10).toFixed(1)}
        </span>
      </div>

      {/* Winner content preview */}
      <p className="text-[11px] leading-relaxed text-muted-foreground line-clamp-2">
        {data.winner.content}
      </p>

      {/* Expand to see all proposals */}
      {data.proposals.length > 1 && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-1 text-[10px] text-muted-foreground/70 hover:text-muted-foreground"
        >
          <ChevronRight
            className={cn(
              "h-3 w-3 transition-transform",
              expanded && "rotate-90",
            )}
          />
          {data.proposals.length} propuestas · {data.rounds_used} ronda{data.rounds_used > 1 ? "s" : ""}
        </button>
      )}

      {expanded && (
        <div className="mt-1 flex flex-col gap-1.5">
          {data.proposals.map((p, i) => (
            <div
              key={i}
              className={cn(
                "border-l-2 pl-2 text-[11px]",
                ROLE_COLORS[p.role] ?? "border-l-muted",
                p.role === data.winner?.role && "bg-primary/5 rounded-r",
              )}
            >
              <div className="flex items-center gap-2">
                <span className="font-medium">
                  {ROLE_LABELS[p.role] ?? p.role}
                </span>
                <span className="text-muted-foreground">
                  {(p.score * 10).toFixed(1)}/10
                </span>
              </div>
              <p className="mt-0.5 text-muted-foreground leading-snug">
                {p.content}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
