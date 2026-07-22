import { useEffect, useState } from "react";

import type { WorkItem, WorkNode } from "@/lib/types";

/** Elapsed between two epoch-millisecond instants, as m:ss (h:mm:ss past an hour).
 *  Derived from a start instant rather than counted, so a reconnect that misses
 *  frames still shows the true elapsed time. */
export function formatElapsed(startedAtMs: number, nowMs: number): string {
  const total = Math.max(0, Math.floor((nowMs - startedAtMs) / 1000));
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** The node a run is currently inside, if any. */
export function activeNode(item: WorkItem): WorkNode | undefined {
  return item.nodes?.filter((n) => n.status === "running").at(-1);
}

/** Epoch milliseconds, refreshed every second while `active`. Clocks tick from
 *  this rather than from their own timers so one interval drives every clock. */
export function useTicker(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}
