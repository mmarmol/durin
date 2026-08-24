import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { LifeChip } from "@/components/automations/LifeChip";
import { LiveRunCard } from "@/components/automations/LiveRunCard";
import { RunDetailCard } from "@/components/automations/RunDetailCard";
import { RunHistory } from "@/components/automations/RunHistory";
import { TriggerChips } from "@/components/automations/TriggerChips";
import { ApiError, listAutomationRuns, type AutomationRun, type AutomationSummary } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

/** One automation's detail: live activity for its running runs, then its run
 *  history with drill-in to each run's cause/delivery/approval record — the
 *  observability center the mockup's screens 3+4 describe. Opened from a row
 *  in the list (AutomationsView holds the selection); `onBack` returns there. */
export function DetailView({
  automation,
  onBack,
  onOpenWorkflowRun,
}: {
  automation: AutomationSummary;
  onBack: () => void;
  onOpenWorkflowRun: (workflow: string, runId: string) => void;
}) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [runs, setRuns] = useState<AutomationRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<AutomationRun | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const got = await listAutomationRuns(token, automation.name);
      setRuns(got);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [token, automation.name]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const anyRunning = useMemo(() => runs.some((r) => r.status === "running"), [runs]);

  // While this automation has a run in flight, keep the run list fresh —
  // AutomationRun.status only ever changes via a fresh fetch (it is not
  // derived from the live workflow_progress frames LiveRunCard consumes), so
  // without this a finished run would stay parked under "running now"
  // forever. Same cadence/pattern as RunsView.tsx's own anyRunning poll.
  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => void refresh(), 4000);
    return () => clearInterval(id);
  }, [anyRunning, refresh]);

  const runningRuns = useMemo(() => runs.filter((r) => r.status === "running"), [runs]);

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <button
          type="button"
          onClick={onBack}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          {t("automations.form.back")}
        </button>
        <span className="text-xs font-medium text-foreground/80">{automation.name}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-5xl flex-col gap-3 px-4 py-4">
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12px] text-muted-foreground">
            <TriggerChips triggers={automation.triggers} />
            <span>
              {t("automations.list.runs")}{" "}
              <b className="font-medium text-foreground/90">{automation.workflow}</b>
            </span>
            <LifeChip automation={automation} />
          </div>
          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> {t("automations.list.loading")}
            </div>
          ) : (
            <>
              {runningRuns.length > 0 && (
                <div className="flex flex-col gap-3">
                  {runningRuns.map((run) => (
                    <LiveRunCard key={run.run_id} run={run} workflow={automation.workflow} />
                  ))}
                </div>
              )}
              <div className="flex w-full gap-3">
                <div
                  className={cn(
                    "min-h-0 flex-col lg:flex lg:w-[340px] lg:shrink-0",
                    selectedRun ? "hidden" : "flex",
                  )}
                >
                  <RunHistory
                    runs={runs}
                    selectedRunId={selectedRun?.run_id ?? null}
                    onSelect={setSelectedRun}
                  />
                </div>
                <div
                  className={cn(
                    "min-h-0 min-w-0 flex-1 flex-col",
                    selectedRun ? "flex" : "hidden lg:flex",
                  )}
                >
                  {selectedRun ? (
                    <RunDetailCard
                      run={selectedRun}
                      automation={automation}
                      onOpenWorkflowRun={onOpenWorkflowRun}
                    />
                  ) : (
                    <div className="flex h-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-10 text-center">
                      <p className="text-[12.5px] text-muted-foreground">
                        {t("automations.detail.history.selectPrompt")}
                      </p>
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
