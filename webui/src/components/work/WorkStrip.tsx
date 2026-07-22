import { useEffect, useRef, useState } from "react";
import { Check, HelpCircle, Loader2, PanelRight, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { activeNode, formatElapsed, touchedNodeCount, useTicker } from "@/lib/work-format";
import type { WorkItem } from "@/lib/types";

/** How long the finished/failed flash stays up after the last active item ends. */
const FLASH_MS = 6000;

/**
 * Slim status line docked above the composer — the work panel's collapsed
 * representation. ThreadShell renders it only while the panel is CLOSED: with
 * the panel open the panel itself is the indicator, so the strip never
 * duplicates it.
 *
 * Reads the work state in priority order:
 *  - any item needs input → warn-tinted "<label> needs your response" + Respond
 *  - anything running     → neutral "<label> · in progress" (a count when >1);
 *    for a single item with a node currently running, the in-progress text is
 *    replaced by "<node label> · <live clock> · <n> nodes" so the strip keeps
 *    changing during a long node instead of sitting on "in progress" for minutes
 *  - active just emptied  → transient finished/failed flash, then nothing
 *
 * The whole strip is one button that opens the work panel.
 */
export function WorkStrip({
  active,
  finished,
  onOpen,
}: {
  active: WorkItem[];
  finished: WorkItem[];
  onOpen: () => void;
}): JSX.Element | null {
  const { t } = useTranslation();

  // Flash: when the active list empties while mounted, keep the most recently
  // finished item visible briefly so the ending is noticed. The finished list
  // is read through a ref so the 4-second poll (new array identity, same
  // content) neither re-arms nor cancels the timer.
  const [flash, setFlash] = useState<WorkItem | null>(null);
  const finishedRef = useRef(finished);
  finishedRef.current = finished;
  const prevActiveCountRef = useRef(active.length);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const prevCount = prevActiveCountRef.current;
    prevActiveCountRef.current = active.length;
    if (active.length > 0 || prevCount === 0) return;
    const last = finishedRef.current[0];
    if (!last) return;
    setFlash(last);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setFlash(null), FLASH_MS);
  }, [active.length]);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  const needsInput = active.filter((w) => w.status === "needs_input");
  const running = active.length > 0;
  const shown = running ? null : flash;
  const warn = needsInput.length > 0;
  // Only meaningful for the single-item, non-warn case (see body below);
  // computed here, ahead of the early return, so the ticker hook always runs
  // unconditionally. Gated on `!warn` too: WorkNode has no "paused" state, so
  // the node a needs_input run is blocked on most plausibly still carries
  // status: "running" with a startedAt — and the warn branch below never
  // reads `node`/`now`, so without this gate the 1-second ticker would keep
  // running for as long as the human takes to answer.
  const node = !warn && active.length === 1 ? activeNode(active[0]) : undefined;
  const now = useTicker(node?.startedAt != null);
  if (!running && !shown) return null;

  let icon: JSX.Element;
  let body: JSX.Element;
  if (warn) {
    icon = <HelpCircle className="h-3.5 w-3.5 shrink-0" aria-hidden />;
    body = (
      <span className="truncate">
        {needsInput.length === 1
          ? t("work.strip.needsInput", { label: needsInput[0].label })
          : t("work.strip.needsInputMany", { count: needsInput.length })}
      </span>
    );
  } else if (running) {
    icon = <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden />;
    // Falls back to the plain status text when nothing is running yet (no
    // node list, or none of the nodes have started) — old behavior preserved.
    const runningSuffix =
      node && node.startedAt != null
        ? `${node.label ?? node.id} · ${formatElapsed(node.startedAt * 1000, now)} · ${t(
            "work.strip.nodes",
            { count: touchedNodeCount(active[0]) },
          )}`
        : t("work.strip.statusRunning");
    body =
      active.length === 1 ? (
        <>
          <span className="truncate font-medium text-foreground/90">
            {active[0].label}
          </span>
          <span className="shrink-0"> · {runningSuffix}</span>
        </>
      ) : (
        <span className="truncate">
          {t("work.strip.runningMany", { count: active.length })}
        </span>
      );
  } else {
    const failed = shown!.status === "failed";
    icon = failed ? (
      <X className="h-3.5 w-3.5 shrink-0 text-destructive" aria-hidden />
    ) : (
      <Check className="h-3.5 w-3.5 shrink-0 text-emerald-500" aria-hidden />
    );
    body = (
      <>
        <span className="truncate font-medium text-foreground/90">
          {shown!.label}
        </span>
        <span className={cn("shrink-0", failed && "text-destructive")}>
          {" "}
          · {failed ? t("work.strip.statusFailed") : t("work.strip.statusDone")}
        </span>
      </>
    );
  }

  return (
    <div role="status" className="mb-2">
      <button
        type="button"
        onClick={onOpen}
        className={cn(
          "flex w-full items-center gap-2 rounded-lg border px-3 py-1.5 text-[12.5px] transition-colors",
          "animate-in fade-in-0 slide-in-from-bottom-1 duration-200",
          warn
            ? "border-warn/60 bg-warn/10 text-warn hover:bg-warn/15"
            : "border-border/60 bg-muted/30 text-muted-foreground hover:bg-muted/60",
        )}
      >
        {icon}
        <span className="flex min-w-0 flex-1 items-baseline text-left">{body}</span>
        <span className="inline-flex shrink-0 items-center gap-1 text-[11.5px]">
          {warn ? t("work.strip.respond") : t("work.strip.view")}
          <PanelRight className="h-3.5 w-3.5" aria-hidden />
        </span>
      </button>
    </div>
  );
}
