import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { BoardView } from "@/components/loops/BoardView";
import { AnswerInput, RetryButton, WaitingAnswerToggle } from "@/components/loops/RunControls";
import { RunDetail } from "@/components/loops/RunDetail";
import { ApiError, answerLoopRun, fireLoop, listAllLoopRuns, type LoopRun } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

// Theme tokens only, no hardcoded colors: needs_operator gets the same accent
// treatment as other actionable items in the app; waiting_info reads as a
// distinct, quieter tone (primary tint, not the accent surface) since it's
// waiting on the counterpart rather than the operator; escalated/error read
// as destructive; everything else (running, done, no_goal) is quiet muted text.
function statusTone(status: LoopRun["status"]): string {
  if (status === "needs_operator") return "bg-accent text-accent-foreground";
  if (status === "waiting_info") return "bg-primary/15 text-primary";
  if (status === "escalated" || status === "error") return "text-destructive";
  return "text-muted-foreground";
}

function AnswerRow({
  run,
  onAnswer,
  answering,
  onToggle,
}: {
  run: LoopRun;
  onAnswer: (run: LoopRun, answer: string) => Promise<boolean>;
  answering: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      className="flex flex-col gap-1.5 rounded-md border border-accent bg-accent/40 px-3 py-2 text-accent-foreground"
      onClick={onToggle}
    >
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="font-mono font-medium">{run.loop}</span>
        <span className="text-[10px] opacity-70">{run.source}</span>
        {!!run.started_at && (
          <span className="text-[10px] opacity-70">{relativeTime(run.started_at * 1000)}</span>
        )}
      </div>
      {run.ask && <div className="whitespace-pre-wrap break-words text-xs">{run.ask}</div>}
      <AnswerInput run={run} onAnswer={onAnswer} answering={answering} />
    </div>
  );
}

// A run parked waiting for the counterpart (channel sender) to reply. The
// ask is shown read-only — the counterpart answers, not the operator — but
// an "answer as operator" toggle reveals the same input as an override.
function WaitingInfoRow({
  run,
  onAnswer,
  answering,
  onToggle,
}: {
  run: LoopRun;
  onAnswer: (run: LoopRun, answer: string) => Promise<boolean>;
  answering: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-border px-3 py-2 text-xs" onClick={onToggle}>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono font-medium">{run.loop}</span>
        <span className={cn("rounded-full px-1.5 py-0.5 text-[10px]", statusTone(run.status))}>
          {t("loops.activity.status.waiting_info")}
        </span>
        <span className="text-[10px] text-muted-foreground">{run.source}</span>
        {!!run.started_at && (
          <span className="text-[10px] text-muted-foreground">{relativeTime(run.started_at * 1000)}</span>
        )}
      </div>
      {run.ask && <div className="whitespace-pre-wrap break-words text-foreground/80">{run.ask}</div>}
      <WaitingAnswerToggle run={run} onAnswer={onAnswer} answering={answering} />
    </div>
  );
}

function RunRow({
  run,
  onRetry,
  retrying,
  onToggle,
}: {
  run: LoopRun;
  onRetry: (run: LoopRun) => void;
  retrying: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const label = run.task || run.run_id.slice(0, 8);
  return (
    <div className="flex flex-col gap-0.5 rounded-md border border-border px-3 py-2 text-xs" onClick={onToggle}>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono font-medium">{run.loop}</span>
        <span className="text-muted-foreground">·</span>
        <span className="truncate text-muted-foreground">{label}</span>
        <span className={cn("rounded-full px-1.5 py-0.5 text-[10px]", statusTone(run.status))}>
          {t("loops.activity.status." + run.status, run.status)}
        </span>
        <span className="text-[10px] text-muted-foreground">{run.source}</span>
        {!!run.started_at && (
          <span className="text-[10px] text-muted-foreground">{relativeTime(run.started_at * 1000)}</span>
        )}
        {run.status === "escalated" && <RetryButton run={run} onRetry={onRetry} retrying={retrying} className="ml-auto" />}
      </div>
    </div>
  );
}

const ACTIVITY_VIEW_KEY = "durin.loops.activityView";
type ActivityViewMode = "list" | "board";

function readStoredView(): ActivityViewMode {
  try {
    return localStorage.getItem(ACTIVITY_VIEW_KEY) === "board" ? "board" : "list";
  } catch {
    return "list";
  }
}

function ViewToggle({ view, onChange }: { view: ActivityViewMode; onChange: (v: ActivityViewMode) => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex h-7 rounded-full bg-muted p-0.5" role="group" aria-label={t("loops.tab.activity")}>
      {(["list", "board"] as const).map((opt) => (
        <button
          key={opt}
          type="button"
          onClick={() => onChange(opt)}
          aria-pressed={view === opt}
          className={cn(
            "rounded-full px-3 text-[12.5px] font-medium transition-colors",
            view === opt ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
          )}
        >
          {opt === "list" ? t("loops.activity.viewList") : t("loops.activity.viewBoard")}
        </button>
      ))}
    </div>
  );
}

export function ActivityView() {
  const { token } = useClient();
  const { t } = useTranslation();
  const [runs, setRuns] = useState<LoopRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [answeringId, setAnsweringId] = useState<string | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [view, setView] = useState<ActivityViewMode>(readStoredView);

  useEffect(() => {
    try {
      localStorage.setItem(ACTIVITY_VIEW_KEY, view);
    } catch {
      // ignore
    }
  }, [view]);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const got = await listAllLoopRuns(token);
      setRuns(got);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 30_000);
    return () => clearInterval(id);
  }, [refresh]);

  // needs_operator rows first (they need a human), then most-recent first.
  const sorted = [...runs].sort((a, b) => {
    const aNeeds = a.status === "needs_operator" ? 0 : 1;
    const bNeeds = b.status === "needs_operator" ? 0 : 1;
    if (aNeeds !== bNeeds) return aNeeds - bNeeds;
    return b.started_at - a.started_at;
  });

  const onAnswer = useCallback(
    async (run: LoopRun, answer: string): Promise<boolean> => {
      if (!answer.trim()) return false;
      setAnsweringId(run.run_id);
      setError(null);
      try {
        await answerLoopRun(token, run.loop, run.run_id, answer);
        await refresh();
        return true;
      } catch (e) {
        setError(errMsg(e));
        return false;
      } finally {
        setAnsweringId(null);
      }
    },
    [token, refresh],
  );

  const onToggle = useCallback((runId: string) => {
    setExpandedId((prev) => (prev === runId ? null : runId));
  }, []);

  const onRetry = useCallback(
    async (run: LoopRun) => {
      setRetryingId(run.run_id);
      setError(null);
      try {
        await fireLoop(token, run.loop);
        await refresh();
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setRetryingId(null);
      }
    },
    [token, refresh],
  );

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <div className="flex items-center justify-end border-b px-3 py-2">
        <ViewToggle view={view} onChange={setView} />
      </div>
      <div className="flex min-h-0 flex-1 overflow-y-auto">
        {view === "list" ? (
          <div className="mx-auto flex w-full max-w-2xl flex-col gap-2 px-4 py-4">
            {error && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                {error}
              </div>
            )}
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> {t("loops.activity.loading")}
              </div>
            ) : sorted.length === 0 ? (
              <p className="text-xs text-muted-foreground">{t("loops.activity.empty")}</p>
            ) : (
              sorted.map((run) => (
                <div key={run.run_id} className="flex flex-col gap-0.5">
                  {run.status === "needs_operator" ? (
                    <AnswerRow
                      run={run}
                      onAnswer={onAnswer}
                      answering={answeringId === run.run_id}
                      onToggle={() => onToggle(run.run_id)}
                    />
                  ) : run.status === "waiting_info" ? (
                    <WaitingInfoRow
                      run={run}
                      onAnswer={onAnswer}
                      answering={answeringId === run.run_id}
                      onToggle={() => onToggle(run.run_id)}
                    />
                  ) : (
                    <RunRow
                      run={run}
                      onRetry={onRetry}
                      retrying={retryingId === run.run_id}
                      onToggle={() => onToggle(run.run_id)}
                    />
                  )}
                  {expandedId === run.run_id && (
                    <div className="rounded-md border border-border/60 bg-muted/20 px-3 py-2">
                      <RunDetail run={run} />
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        ) : (
          <div className="flex w-full flex-col gap-2 px-4 py-4">
            {error && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                {error}
              </div>
            )}
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> {t("loops.activity.loading")}
              </div>
            ) : sorted.length === 0 ? (
              <p className="text-xs text-muted-foreground">{t("loops.activity.empty")}</p>
            ) : (
              <BoardView
                runs={sorted}
                onAnswer={onAnswer}
                answeringId={answeringId}
                onRetry={onRetry}
                retryingId={retryingId}
                expandedId={expandedId}
                onToggle={onToggle}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
