import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { RunDetail, runChipTone } from "@/components/workflows/RunDetail";
import {
  ApiError,
  getWorkflowRunManifest,
  listAllWorkflowRuns,
  runWorkflow,
  type WorkflowGlobalRun,
  type WorkflowRunResult,
} from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

const STATUS_FILTERS = ["all", "completed", "needs_input", "exhausted", "aborted", "crashed", "cancelled"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

const selectCls = "h-8 rounded-md border border-border bg-background px-2 text-xs";

// Every stranded needs_input run that is actually resumable — the ones this tab's
// tray (and the Workflows sidebar badge) surfaces so a user can resume them without
// hunting down which workflow/session they belong to. A needs_input run written
// before the resume feature shipped has no needs_input_node and can't be resumed;
// it still shows up in the ordinary feed, just not here.
export function strandedRuns(runs: WorkflowGlobalRun[]): WorkflowGlobalRun[] {
  return runs.filter((r) => r.status === "needs_input" && !!r.needs_input_node);
}

function TrayEntry({
  entry,
  onResume,
  resuming,
}: {
  entry: WorkflowGlobalRun;
  onResume: (entry: WorkflowGlobalRun, answers: string) => void;
  resuming: boolean;
}) {
  const { t } = useTranslation();
  const [answers, setAnswers] = useState("");
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-accent bg-accent/40 px-3 py-2 text-accent-foreground">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="font-mono font-medium">{entry.workflow}</span>
        {!!entry.started_at && (
          <span className="text-[10px] opacity-70">
            {t("runs.pausedAt", { when: relativeTime(entry.started_at * 1000) })}
          </span>
        )}
      </div>
      {entry.questions && (
        <div className="whitespace-pre-wrap break-words text-xs">{entry.questions}</div>
      )}
      <Textarea
        rows={2}
        value={answers}
        onChange={(e) => setAnswers(e.target.value)}
        placeholder={t("workflows.answersPlaceholder")}
        className="bg-background text-foreground"
      />
      <Button
        size="sm"
        className="self-start"
        disabled={resuming || !answers.trim()}
        onClick={() => onResume(entry, answers)}
      >
        {resuming ? <Loader2 className="h-4 w-4 animate-spin" /> : t("workflows.resumeRun")}
      </Button>
    </div>
  );
}

function FeedRow({
  entry,
  indent,
  active,
  onClick,
}: {
  entry: WorkflowGlobalRun;
  indent: boolean;
  active: boolean;
  onClick: () => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full flex-col gap-0.5 rounded-md border px-3 py-2 text-left text-xs",
        indent && "ml-4",
        active ? "border-primary" : "border-border hover:bg-muted/60",
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono font-medium">{entry.workflow}</span>
        <span className={cn("rounded border px-1 py-0.5 text-[10px]", runChipTone(entry.status))}>
          {t("workflows.runStatus." + entry.status, entry.status)}
        </span>
        {!!entry.started_at && (
          <span className="text-[10px] text-muted-foreground">
            {relativeTime(entry.started_at * 1000)}
          </span>
        )}
      </div>
      {entry.task && (
        <span className="truncate text-[11px] text-muted-foreground">{entry.task}</span>
      )}
    </button>
  );
}

export function RunsView() {
  const { token } = useClient();
  const { t } = useTranslation();
  const [runs, setRuns] = useState<WorkflowGlobalRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [workflowFilter, setWorkflowFilter] = useState<string>("all");
  const [selected, setSelected] = useState<WorkflowGlobalRun | null>(null);
  const [manifest, setManifest] = useState<WorkflowRunResult | null>(null);
  const [manifestLoading, setManifestLoading] = useState(false);
  const [resumingId, setResumingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const got = await listAllWorkflowRuns(token);
      setRuns(got);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const tray = useMemo(() => strandedRuns(runs), [runs]);

  const workflowNames = useMemo(
    () => Array.from(new Set(runs.map((r) => r.workflow))).sort(),
    [runs],
  );

  const feed = useMemo(() => {
    return runs.filter((r) => {
      if (statusFilter !== "all" && r.status !== statusFilter) return false;
      if (workflowFilter !== "all" && r.workflow !== workflowFilter) return false;
      return true;
    });
  }, [runs, statusFilter, workflowFilter]);

  const byId = useMemo(() => new Map(feed.map((r) => [r.run_id, r])), [feed]);

  const onResume = useCallback(
    async (entry: WorkflowGlobalRun, answers: string) => {
      if (!answers.trim()) return;
      setResumingId(entry.run_id);
      setError(null);
      try {
        await runWorkflow(token, entry.workflow, answers, [], "", "", entry.run_id);
        await refresh();
        setSelected(null);
        setManifest(null);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setResumingId(null);
      }
    },
    [token, refresh],
  );

  const onSelectEntry = useCallback(
    async (entry: WorkflowGlobalRun) => {
      setSelected(entry);
      setManifest(null);
      setManifestLoading(true);
      setError(null);
      try {
        const got = await getWorkflowRunManifest(token, entry.workflow, entry.run_id);
        setManifest(got);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setManifestLoading(false);
      }
    },
    [token],
  );

  const onResumeFromDetail = useCallback(
    (answers: string) => {
      if (!selected) return;
      void onResume(selected, answers);
    },
    [selected, onResume],
  );

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {/* No panel title here — the Workflows/Runs pane switcher above already
          names this view; repeating it was pure noise. The first row is a
          toolbar: run count (orientation) + filters. */}
      <div className="flex min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-2xl flex-col gap-3 px-4 py-4">
          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> {t("workflows.loading")}
            </div>
          ) : (
            <>
              {tray.length > 0 && (
                <div className="flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                      {t("runs.trayTitle")}
                    </h2>
                    <span className="rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-medium text-accent-foreground">
                      {tray.length}
                    </span>
                  </div>
                  {tray.map((entry) => (
                    <TrayEntry
                      key={entry.run_id}
                      entry={entry}
                      onResume={onResume}
                      resuming={resumingId === entry.run_id}
                    />
                  ))}
                </div>
              )}

              <div className="flex flex-wrap items-center gap-2">
                <span className="flex-1 text-xs text-muted-foreground">
                  {t("runs.countLabel", { count: feed.length })}
                </span>
                <select
                  value={statusFilter}
                  onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
                  className={selectCls}
                  aria-label={t("runs.statusFilterAria")}
                >
                  {STATUS_FILTERS.map((s) => (
                    <option key={s} value={s}>
                      {s === "all" ? t("runs.statusAll") : t("workflows.runStatus." + s, s)}
                    </option>
                  ))}
                </select>
                <select
                  value={workflowFilter}
                  onChange={(e) => setWorkflowFilter(e.target.value)}
                  className={selectCls}
                  aria-label={t("runs.workflowFilterAria")}
                >
                  <option value="all">{t("runs.allWorkflows")}</option>
                  {workflowNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="flex flex-col gap-1.5">
                {feed.length === 0 && (
                  <p className="text-xs text-muted-foreground">{t("runs.empty")}</p>
                )}
                {feed.map((entry) => {
                  const parentInList = entry.parent_run_id != null && byId.has(entry.parent_run_id);
                  return (
                    <div key={entry.run_id} className="flex flex-col gap-0.5">
                      {entry.parent_run_id && !parentInList && (
                        <span className="ml-4 text-[10px] text-muted-foreground opacity-70">
                          {t("runs.subOf", { id: entry.parent_run_id })}
                        </span>
                      )}
                      <FeedRow
                        entry={entry}
                        indent={!!entry.parent_run_id}
                        active={selected?.run_id === entry.run_id}
                        onClick={() =>
                          selected?.run_id === entry.run_id
                            ? setSelected(null)
                            : void onSelectEntry(entry)
                        }
                      />
                      {selected?.run_id === entry.run_id && (
                        <div className="mb-1 mt-0.5 rounded-md border p-3 text-xs">
                          <div className="mb-2 font-mono text-[11px] text-muted-foreground">
                            {selected.workflow} · {selected.run_id}
                          </div>
                          {manifestLoading ? (
                            <div className="flex items-center gap-2 text-muted-foreground">
                              <Loader2 className="h-4 w-4 animate-spin" /> {t("workflows.loading")}
                            </div>
                          ) : (
                            manifest && (
                              <RunDetail
                                result={manifest}
                                resuming={resumingId === selected.run_id}
                                onResume={onResumeFromDetail}
                              />
                            )
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
