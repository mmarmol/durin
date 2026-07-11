import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, answerLoopRun, fireLoop, listAllLoopRuns, type LoopRun } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

// Theme tokens only, no hardcoded colors: needs_operator gets the same accent
// treatment as other actionable items in the app, escalated/error read as
// destructive, everything else (running, done, no_goal) is quiet muted text.
function statusTone(status: LoopRun["status"]): string {
  if (status === "needs_operator") return "bg-accent text-accent-foreground";
  if (status === "escalated" || status === "error") return "text-destructive";
  return "text-muted-foreground";
}

function AnswerRow({
  run,
  onAnswer,
  answering,
}: {
  run: LoopRun;
  onAnswer: (run: LoopRun, answer: string) => Promise<boolean>;
  answering: boolean;
}) {
  const { t } = useTranslation();
  const [answer, setAnswer] = useState("");
  const [sent, setSent] = useState(false);

  const handleSend = useCallback(async () => {
    if (answering) return;
    const text = answer.trim();
    if (!text) return;
    setAnswer("");
    const ok = await onAnswer(run, text);
    if (ok) {
      setSent(true);
    } else {
      // Restore the typed answer so the user can retry instead of retyping it.
      setAnswer(text);
    }
  }, [answering, answer, run, onAnswer]);

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-accent bg-accent/40 px-3 py-2 text-accent-foreground">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="font-mono font-medium">{run.loop}</span>
        <span className="text-[10px] opacity-70">{run.source}</span>
        {!!run.started_at && (
          <span className="text-[10px] opacity-70">{relativeTime(run.started_at * 1000)}</span>
        )}
      </div>
      {run.ask && <div className="whitespace-pre-wrap break-words text-xs">{run.ask}</div>}
      {sent ? (
        <div className="text-xs text-muted-foreground">{t("loops.activity.answerSent")}</div>
      ) : (
        <div className="flex gap-1.5">
          <Input
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder={t("loops.activity.answerPlaceholder")}
            className="h-8 bg-background text-foreground"
            disabled={answering}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleSend();
            }}
          />
          <Button
            size="sm"
            disabled={answering || !answer.trim()}
            onClick={() => void handleSend()}
          >
            {answering ? <Loader2 className="h-4 w-4 animate-spin" /> : t("loops.activity.send")}
          </Button>
        </div>
      )}
    </div>
  );
}

function RunRow({
  run,
  onRetry,
  retrying,
}: {
  run: LoopRun;
  onRetry: (run: LoopRun) => void;
  retrying: boolean;
}) {
  const { t } = useTranslation();
  const label = run.task || run.run_id.slice(0, 8);
  return (
    <div className="flex flex-col gap-0.5 rounded-md border border-border px-3 py-2 text-xs">
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
        {run.status === "escalated" && (
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto h-6 gap-1 px-2 text-[11px]"
            disabled={retrying}
            onClick={() => onRetry(run)}
          >
            {retrying ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
            {t("loops.activity.retry")}
          </Button>
        )}
      </div>
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
      <div className="flex min-h-0 flex-1 overflow-y-auto">
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
            sorted.map((run) =>
              run.status === "needs_operator" ? (
                <AnswerRow
                  key={run.run_id}
                  run={run}
                  onAnswer={onAnswer}
                  answering={answeringId === run.run_id}
                />
              ) : (
                <RunRow
                  key={run.run_id}
                  run={run}
                  onRetry={onRetry}
                  retrying={retryingId === run.run_id}
                />
              ),
            )
          )}
        </div>
      </div>
    </div>
  );
}
