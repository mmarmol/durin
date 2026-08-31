import { useTranslation } from "react-i18next";

import type { AutomationRun } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { formatElapsed } from "@/lib/work-format";
import { cn } from "@/lib/utils";

// Every fire(source=...) call site is where a run's cause.kind is set —
// verified against the actual call sites, not the field's plausible-looking
// name: "schedule" (durin/cli/commands.py's cron-tick dispatch — NOT "cron";
// AutomationsRuntime's own retry path falls back to the literal "cron" only
// for a pre-existing orphaned run whose manifest predates cause tracking, a
// legacy case this map does not special-case), "channel" (an ordinary
// inbound channel message), "webhook" (durin/automations/matcher.py's _fire,
// for a HookDispatcher-origin fire — distinguished from "channel" by that
// function passing its real channel through, not the literal "channel"
// string, for exactly this reason), "chain", "manual" (the dashboard's "run
// now"), and "chat" (the agent tool, fired from a conversation). An unmapped
// kind renders with no icon rather than a guessed one.
const CAUSE_ICONS: Record<string, string> = {
  schedule: "⏰",
  channel: "💬",
  webhook: "🪝",
  chain: "⛓",
  manual: "⚙",
  chat: "💬",
};

/** The glyph the mockup uses for a run's trigger kind, shared by LiveRunCard's
 *  cause line and this file's own cause excerpt — reused rather than
 *  reimplemented per component, since both read the exact same field. */
// `kind` is optional armor, not an expected state: the backend normalizes
// legacy loops-era records on read, but a render must never white-screen the
// whole app over one malformed row.
export function causeIcon(kind: string | undefined): string {
  return (kind && CAUSE_ICONS[kind]) || "";
}

// Mirrors NeedsYouTray's own capText (webui/src/components/automations/NeedsYouTray.tsx)
// — same shape, kept local rather than imported since that file has no reason
// to export a display-truncation helper for an unrelated card.
function capText(text: string, max: number): string {
  const trimmed = text.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

const EXCERPT_CAP = 140;

// The chip's tone mapping mirrors RunDots.toneFor exactly (same file, same
// automations/RunDots.tsx): achieved/completed read as "it worked", failed is
// a hard failure, rejected/interrupted are a deliberate stop (not a failure)
// so they get the warn tone, running and paused share the accent tone (paused
// omits the pulse there — a chip has no pulse to omit, the words already say
// "paused" or "running"). This is the "C2 color language" the outcome chip is
// meant to reuse rather than invent a second palette.
export function outcomeChipCls(status: AutomationRun["status"]): string {
  if (status === "achieved" || status === "completed") return "bg-primary/15 text-primary";
  if (status === "failed") return "bg-destructive/15 text-destructive";
  if (status === "rejected" || status === "interrupted") return "bg-warn/15 text-warn";
  return "bg-accent/15 text-accent"; // running | paused
}

function HistoryRow({
  run,
  selected,
  onSelect,
}: {
  run: AutomationRun;
  selected: boolean;
  onSelect: (run: AutomationRun) => void;
}) {
  const { t } = useTranslation();
  const icon = causeIcon(run.cause?.kind);
  const excerpt = capText(run.cause?.excerpt ?? "", EXCERPT_CAP);
  const durationS =
    run.finished_at != null && run.started_at != null ? run.finished_at - run.started_at : null;
  // The compact delivery line, or null when there is nothing to show —
  // delivery is null on every run that has not reached the delivery step yet
  // (see AutomationRun's own field comment), not just a silenced one.
  // RunDetailCard renders the same record in its own, richer form
  // (channel/to/at + result), so this one-liner stays local to this row.
  const delivery = run.delivery
    ? `→ ${run.delivery.channel} · ${t(`automations.detail.deliveryResult.${run.delivery.result}`, run.delivery.result)}`
    : null;

  return (
    <button
      type="button"
      data-testid="run-history-row"
      aria-current={selected ? "true" : undefined}
      onClick={() => onSelect(run)}
      className={cn(
        "flex w-full flex-col gap-0.5 border-t border-border px-3.5 py-2 text-left first:border-t-0",
        selected ? "bg-accent/40" : "hover:bg-muted/50",
      )}
    >
      <span className="flex w-full items-center gap-2">
        <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-medium", outcomeChipCls(run.status))}>
          {t(`automations.list.runStatus.${run.status}`, run.status)}
        </span>
        <span className="ml-auto shrink-0 text-[10.5px] text-muted-foreground">
          {relativeTime(run.started_at * 1000)}
        </span>
      </span>
      <span className="truncate text-[12px] text-muted-foreground">
        {icon ? `${icon} ` : ""}
        {excerpt}
        {durationS != null && ` · ${formatElapsed(0, durationS * 1000)}`}
      </span>
      {delivery && <span className="truncate text-[11.5px] text-muted-foreground/80">{delivery}</span>}
    </button>
  );
}

/** Per-automation run history, newest-first: the mockup's screen 4 left pane.
 *  Selecting a row is the caller's job to react to (opening RunDetailCard) —
 *  this component only renders the list and reports the click. */
export function RunHistory({
  runs,
  selectedRunId,
  onSelect,
}: {
  runs: AutomationRun[];
  selectedRunId: string | null;
  onSelect: (run: AutomationRun) => void;
}) {
  const { t } = useTranslation();
  const sorted = [...runs].sort((a, b) => b.started_at - a.started_at);

  return (
    <div className="rounded-lg border border-border">
      <div className="flex items-center justify-between border-b border-border px-3.5 py-2.5 text-[13px] font-semibold">
        {t("automations.detail.history.title")}
        <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-normal text-muted-foreground">
          {sorted.length}
        </span>
      </div>
      {sorted.length === 0 ? (
        <p className="px-3.5 py-3 text-xs text-muted-foreground">{t("automations.detail.history.empty")}</p>
      ) : (
        <div className="flex flex-col">
          {sorted.map((run) => (
            <HistoryRow key={run.run_id} run={run} selected={run.run_id === selectedRunId} onSelect={onSelect} />
          ))}
        </div>
      )}
    </div>
  );
}
