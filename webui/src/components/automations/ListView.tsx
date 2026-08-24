import { useTranslation } from "react-i18next";

import { LifeChip } from "@/components/automations/LifeChip";
import { RunDots } from "@/components/automations/RunDots";
import { TriggerChips } from "@/components/automations/TriggerChips";
import type { AutomationRun, AutomationSummary, CronJobRow } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";

/** The earliest upcoming fire among the cron jobs B7's sync created for this
 *  automation's own schedule triggers (an automation can have more than
 *  one), or null when none matches — a channel/webhook/chain-only
 *  automation, or one whose schedule trigger hasn't been synced into a cron
 *  job yet. */
export function nextFireAtMs(name: string, cronJobs: CronJobRow[]): number | null {
  const ms = cronJobs
    .filter((job) => job.automation === name && job.state.next_run_at_ms != null)
    .map((job) => job.state.next_run_at_ms as number);
  return ms.length === 0 ? null : Math.min(...ms);
}

/** This automation's own runs, newest first — the source both the running
 *  count and RunDots' last-5 strip read from. */
export function runsForAutomation(name: string, runs: AutomationRun[]): AutomationRun[] {
  return runs.filter((r) => r.automation === name).sort((a, b) => b.started_at - a.started_at);
}

export function ListView({
  automations,
  runs,
  cronJobs,
}: {
  automations: AutomationSummary[];
  runs: AutomationRun[];
  cronJobs: CronJobRow[];
}) {
  const { t } = useTranslation();
  return (
    <div className="rounded-lg border border-border">
      <div className="flex items-center justify-between border-b border-border px-3.5 py-2.5 text-[13px] font-semibold">
        {t("automations.list.title")}
        <span className="text-[12px] font-normal text-muted-foreground">
          {t("automations.list.count", { count: automations.length })}
        </span>
      </div>
      {automations.length === 0 ? (
        <p className="px-3.5 py-3 text-xs text-muted-foreground">{t("automations.list.empty")}</p>
      ) : (
        <div className="flex flex-col">
          {automations.map((def) => {
            const ownRuns = runsForAutomation(def.name, runs);
            const nextMs = nextFireAtMs(def.name, cronJobs);
            return (
              <div
                key={def.name}
                data-testid="automation-row"
                className="flex items-center gap-3 border-t border-border px-3.5 py-2.5 first:border-t-0"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[13.5px] font-medium">{def.name}</div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12px] text-muted-foreground">
                    <TriggerChips triggers={def.triggers} />
                    <span>
                      {t("automations.list.runs")}{" "}
                      <b className="font-medium text-foreground/90">{def.workflow}</b>
                    </span>
                    {nextMs != null && <span>{t("automations.list.nextFire", { when: fmtDateTime(nextMs) })}</span>}
                  </div>
                </div>
                {!def.achieved && <RunDots runs={ownRuns} />}
                {def.active_runs > 0 && (
                  <span className="shrink-0 rounded-full bg-accent/15 px-2 py-0.5 text-[10.5px] font-medium text-accent">
                    {t("automations.list.runningCount", { count: def.active_runs })}
                  </span>
                )}
                <LifeChip automation={def} />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
