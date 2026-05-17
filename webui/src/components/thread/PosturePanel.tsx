import { cn } from "@/lib/utils";
import type { PostureUpdateData } from "@/lib/types";

const AXIS_LABELS: Record<string, string> = {
  cautela: "Cautela",
  exploracion: "Exploración",
  profundidad: "Profundidad",
  disciplina: "Disciplina",
  conformidad: "Conformidad",
};

const AXIS_ORDER = ["cautela", "exploracion", "profundidad", "disciplina", "conformidad"];

interface PosturePanelProps {
  data: PostureUpdateData;
}

function DeltaIndicator({ delta }: { delta: number }) {
  if (Math.abs(delta) < 0.001) return null;
  const isUp = delta > 0;
  return (
    <span
      className={cn(
        "ml-1.5 text-[10px] font-medium",
        isUp ? "text-emerald-500" : "text-rose-400",
      )}
    >
      {isUp ? "+" : ""}{(delta * 100).toFixed(1)}%
    </span>
  );
}

export function PosturePanel({ data }: PosturePanelProps) {
  return (
    <div className="flex flex-col gap-1.5 py-1">
      {AXIS_ORDER.map((axis) => {
        const value = data.axes[axis];
        if (value === undefined) return null;
        const delta = data.deltas[axis] ?? 0;
        const pct = Math.round(value * 100);
        return (
          <div key={axis} className="flex items-center gap-2 text-[11px]">
            <span className="w-[76px] shrink-0 text-muted-foreground">
              {AXIS_LABELS[axis] ?? axis}
            </span>
            <div className="relative h-1.5 flex-1 rounded-full bg-muted/60">
              <div
                className={cn(
                  "absolute inset-y-0 left-0 rounded-full transition-all duration-500",
                  delta > 0.001
                    ? "bg-emerald-500/70"
                    : delta < -0.001
                      ? "bg-rose-400/70"
                      : "bg-primary/50",
                )}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="w-[32px] shrink-0 text-right tabular-nums text-muted-foreground">
              {pct}%
            </span>
            <DeltaIndicator delta={delta} />
          </div>
        );
      })}
    </div>
  );
}
