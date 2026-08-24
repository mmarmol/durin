import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { LifeChip } from "@/components/automations/LifeChip";
import { LiveRunCard } from "@/components/automations/LiveRunCard";
import { RunDetailCard } from "@/components/automations/RunDetailCard";
import { RunHistory } from "@/components/automations/RunHistory";
import { TriggerChips } from "@/components/automations/TriggerChips";
import { Button } from "@/components/ui/button";
import { ApiError, fireAutomation, listAutomationRuns, type AutomationRun, type AutomationSummary } from "@/lib/api";
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
  // The id, not the run object itself: a running selection must keep
  // reflecting the live `runs` array as it refreshes (every 4s while
  // anything is running — see below), not freeze at whatever snapshot was
  // selected. Deriving the object from `runs` on every render, rather than
  // storing it, is what makes that update-in-place happen for free.
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

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
  const selectedRun = useMemo(
    () => (selectedRunId ? (runs.find((r) => r.run_id === selectedRunId) ?? null) : null),
    [runs, selectedRunId],
  );

  // "Ejecutar ahora" (mockup screen 3's header button): fires a fresh manual
  // run of this automation, then refreshes so it shows up under "running
  // now" right away. fireNotice is transient (cleared after a few seconds,
  // same idiom as AutomationsView's own post-resolution confirmation line);
  // fireError sticks around until the next attempt, like the fetch error
  // above.
  const [firing, setFiring] = useState(false);
  const [fireNotice, setFireNotice] = useState<string | null>(null);
  const [fireError, setFireError] = useState<string | null>(null);
  const fireNoticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (fireNoticeTimerRef.current) clearTimeout(fireNoticeTimerRef.current);
    };
  }, []);

  const onFireNow = useCallback(async () => {
    setFiring(true);
    setFireError(null);
    try {
      const { run_id } = await fireAutomation(token, automation.name);
      setFireNotice(t("automations.detail.fireNowStarted", { runId: run_id }));
      if (fireNoticeTimerRef.current) clearTimeout(fireNoticeTimerRef.current);
      fireNoticeTimerRef.current = setTimeout(() => setFireNotice(null), 4000);
      await refresh();
    } catch (e) {
      setFireError(errMsg(e));
    } finally {
      setFiring(false);
    }
  }, [token, automation.name, refresh, t]);

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
        <div className="ml-auto flex items-center gap-2">
          {fireNotice && <span className="text-[11px] text-muted-foreground">{fireNotice}</span>}
          {fireError && <span className="text-[11px] text-destructive">{fireError}</span>}
          <Button size="sm" variant="outline" disabled={firing} onClick={() => void onFireNow()}>
            {firing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : t("automations.detail.fireNow")}
          </Button>
        </div>
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
                    selectedRunId={selectedRunId}
                    onSelect={(run) => setSelectedRunId(run.run_id)}
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
