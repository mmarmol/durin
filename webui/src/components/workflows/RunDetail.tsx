import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  FileIcon,
  HelpCircle,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { WorkflowRunNode, WorkflowRunResult } from "@/lib/api";
import { formatElapsed, useTicker } from "@/lib/work-format";
import { cn } from "@/lib/utils";

/** Sum of every present value, or null if none are present — an absent sum reads as
 *  "no data" rather than a fabricated 0 (e.g. a workflow with no completed runs yet
 *  has no typical durations at all, not a typical of zero). */
function sumKnown(values: Array<number | null | undefined>): number | null {
  const known = values.filter((v): v is number => v != null);
  return known.length > 0 ? known.reduce((a, b) => a + b, 0) : null;
}

function statusTone(status: string): string {
  if (status === "node_failed" || status === "persist_failed") {
    return "bg-destructive/10 text-destructive";
  }
  if (status === "no_session") return "bg-muted text-muted-foreground";
  return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400";
}

// A run's overall status mapped to a history-chip border tone: needs_input draws
// attention (accent), exhausted is a soft warning, aborted is a hard failure, and
// completed (or any other terminal/live status) stays neutral.
export function runChipTone(status: string): string {
  if (status === "needs_input") return "border-accent text-accent-foreground";
  if (status === "exhausted") return "border-warn/60 text-warn";
  if (status === "aborted" || status === "crashed") return "border-destructive/60 text-destructive";
  if (status === "cancelled") return "border-dashed text-muted-foreground";
  return "text-muted-foreground hover:text-foreground";
}

// A workflow node session is headless (not a chat in the sidebar), so there is no
// in-app viewer to open it by key. Surface the key as a labelled, copyable reference
// so an auditor can look the session up by hand.
export function CopyableKey({ value }: { value: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const onCopy = useCallback(() => {
    if (!navigator.clipboard) return;
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1_500);
    });
  }, [value]);
  return (
    <button
      type="button"
      onClick={onCopy}
      title={t("workflows.copySession")}
      aria-label={copied ? t("workflows.sessionCopied") : t("workflows.copySession")}
      className="inline-flex max-w-full items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
    >
      {copied ? <Check className="h-3 w-3 shrink-0" /> : <Copy className="h-3 w-3 shrink-0" />}
      <span className="truncate">{value}</span>
    </button>
  );
}

// One per-node/worker row in a run's trace: identity (node_id#iteration, or
// "pass N of budget" once the node has a known visit budget), fan-out
// worker_index / static branch_id so concurrent units are legible, a status
// chip and route verdict, a "continues session" chip when this row picks up
// an earlier row's session (a resumed/looping node), the copyable session
// key, the node's (truncated) output, and — the wide surface's own columns —
// how long it actually took, how long it typically takes, and what it produced.
export function RunNodeRow({
  run,
  continuesSession,
  typicalS,
}: {
  run: WorkflowRunNode;
  continuesSession: boolean;
  // Median seconds this node took across prior completed runs; absent with no history.
  typicalS?: number;
}) {
  const { t } = useTranslation();
  const verdict =
    run.route_label != null && run.route_label !== ""
      ? run.route_label
      : run.passed === true
        ? "✓"
        : run.passed === false
          ? "✗"
          : null;
  const isFinalPass = run.budget != null && run.iteration === run.budget;

  // "took"/"typical" combined into one line rather than two separate elements:
  // an actual duration can format to the same m:ss as its typical counterpart,
  // and keeping both readings in a single text node avoids showing what looks
  // like one duplicated number in two places.
  const metaParts: string[] = [];
  if (run.duration_s != null) {
    metaParts.push(`${t("workflows.nodeDuration")} ${formatElapsed(0, run.duration_s * 1000)}`);
  }
  if (typicalS != null) {
    metaParts.push(`${t("workflows.nodeTypical")} ${formatElapsed(0, typicalS * 1000)}`);
  }

  return (
    <div className="flex flex-col gap-1 rounded border px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[11px] font-medium">
          {run.budget != null
            ? `${run.node_id} · ${t("workflows.passOf", { iteration: run.iteration, budget: run.budget })}`
            : `${run.node_id}#${run.iteration}`}
        </span>
        {isFinalPass && (
          <span className="rounded bg-warn/10 px-1 py-0.5 text-[10px] text-warn">
            {t("workflows.finalPass")}
          </span>
        )}
        {continuesSession && (
          <span className="inline-flex items-center gap-0.5 rounded bg-accent px-1 py-0.5 text-[10px] text-accent-foreground">
            <RefreshCw className="h-2.5 w-2.5" aria-hidden />
            {t("workflows.continuesSession")}
          </span>
        )}
        {run.worker_index != null && (
          <span className="rounded bg-violet-500/10 px-1 py-0.5 text-[10px] text-violet-700 dark:text-violet-300">
            {t("workflows.workerLabel", { index: run.worker_index })}
          </span>
        )}
        {run.branch_id && (
          <span className="rounded bg-sky-500/10 px-1 py-0.5 text-[10px] text-sky-700 dark:text-sky-300">
            {t("workflows.branchLabel", { id: run.branch_id })}
          </span>
        )}
        <span className={cn("rounded px-1 py-0.5 text-[10px]", statusTone(run.status))}>
          {t("workflows.runStatus." + run.status, run.status)}
        </span>
        {verdict != null && (
          <span
            className={cn(
              "rounded px-1 py-0.5 text-[10px]",
              run.passed === false ? "bg-destructive/10 text-destructive" : "bg-muted",
            )}
          >
            {verdict}
          </span>
        )}
        {run.session_key && <CopyableKey value={run.session_key} />}
      </div>
      {run.output && (
        <div className="whitespace-pre-wrap break-words text-[11px] text-muted-foreground">
          {run.output}
        </div>
      )}
      {metaParts.length > 0 && (
        <div className="text-[10px] text-muted-foreground">{metaParts.join(" · ")}</div>
      )}
      {run.artifacts && run.artifacts.length > 0 && (
        <div className="text-[10px] text-muted-foreground">
          {t("workflows.nodeArtifacts")}: {run.artifacts.join(", ")}
        </div>
      )}
    </div>
  );
}

// Which rows "continue" an earlier row's session: a row whose session_key matches
// an EARLIER row's session_key for the same node_id (a loop pass picking back up
// a persistent session, or a resumed run re-entering the node it stopped at).
function continuesSessionFlags(runs: WorkflowRunNode[]): boolean[] {
  const seen = new Map<string, Set<string>>(); // node_id -> session_keys seen so far
  return runs.map((run) => {
    if (!run.session_key) return false;
    const keys = seen.get(run.node_id);
    const continues = keys != null && keys.has(run.session_key);
    seen.set(run.node_id, new Set(keys).add(run.session_key));
    return continues;
  });
}

// The detail view of a single run: a status banner (needs_input/exhausted/aborted/
// cancelled), a row per node/worker (status, output, session affordance, loop pass),
// the resume form (needs_input only), the final output, output folder and files.
export function RunDetail({
  result,
  onResume,
  resuming,
}: {
  result: WorkflowRunResult;
  onResume: (answers: string) => void;
  resuming: boolean;
}) {
  const { t } = useTranslation();
  const [answers, setAnswers] = useState("");
  const continues = continuesSessionFlags(result.runs);
  const outputFiles = result.output_files ?? [];
  // Only a run that is still running has a node in flight. Crash reconciliation
  // flips a dead run to "crashed" without clearing its active_node marker (only
  // a node's own completion does that), so an ungated read renders a long-dead
  // node as a spinning "running" row whose clock never stops.
  const activeNodeInfo = result.status === "running" ? (result.active_node ?? null) : null;

  // Ticks only while a node is actually in flight, so the header's elapsed total
  // (below) advances live for a running run and freezes once there's nothing left
  // for it to count up from.
  const now = useTicker(activeNodeInfo != null);

  // The run header's totals: actual elapsed (completed nodes' durations, plus the
  // active node's live delta while one is running) alongside the typical total from
  // prior runs — both sums, so they read as a direct comparison. Either is null
  // (rendered as absent, not 0) when there is nothing to sum: an older manifest with
  // no duration data, or a workflow with no completed-run history yet.
  const completedS = sumKnown(result.runs.map((r) => r.duration_s));
  const activeS = activeNodeInfo != null ? Math.max(0, now / 1000 - activeNodeInfo.started_at) : null;
  const elapsedTotalS = completedS != null || activeS != null ? (completedS ?? 0) + (activeS ?? 0) : null;
  // The run's own recorded estimate: the median TOTAL of prior completed runs.
  // Never the sum of the per-node medians — those cover every branch any prior
  // run took, while this run takes one of them.
  const typicalTotalS = result.typical_total_s ?? null;

  // Reset answers when the run identity or needs_input status changes to avoid stale
  // textarea content on nested resume (same component instance with new result props).
  useEffect(() => {
    setAnswers("");
  }, [result.run_id, result.status]);

  return (
    <div className="flex flex-col gap-2">
      {result.status === "needs_input" && (
        <div className="flex flex-col gap-1.5 rounded bg-accent px-2 py-1.5 text-accent-foreground">
          <div className="flex items-center gap-1.5">
            <HelpCircle className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="font-medium">{t("workflows.needsInputTitle")}</span>
          </div>
          <p>
            {t("workflows.needsInputBody", { node: result.needs_input_node || "?" })}
          </p>
          {result.final_output && (
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wide opacity-70">
                {t("workflows.questionsFromRun")}
              </span>
              <div className="whitespace-pre-wrap break-words">{result.final_output}</div>
            </div>
          )}
          {result.needs_input_node && (
            <>
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
                onClick={() => onResume(answers)}
              >
                {resuming ? <Loader2 className="h-4 w-4 animate-spin" /> : t("workflows.resumeRun")}
              </Button>
              <span className="text-[10px] opacity-70">
                {t("workflows.resumeCaption", { node: result.needs_input_node, runId: result.run_id })}
              </span>
            </>
          )}
        </div>
      )}
      {result.status === "exhausted" && (
        <div className="flex flex-col gap-0.5 rounded bg-warn/10 px-2 py-1.5 text-warn">
          <span className="font-medium">{t("workflows.loopLimitReached")}</span>
          <span>
            {t("workflows.exhausted")}
            {result.exhausted_node && (
              <>
                {" "}— {t("workflows.exhaustedNode")}: <span className="font-mono">{result.exhausted_node}</span>
              </>
            )}
          </span>
        </div>
      )}
      {result.status === "aborted" && (
        <div className="flex flex-col gap-1 rounded bg-destructive/10 px-2 py-1.5 text-destructive">
          <div className="flex items-center gap-1.5">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="font-medium">{t("workflows.abortedTitle")}</span>
          </div>
          {result.final_output && <p className="whitespace-pre-wrap break-words">{result.final_output}</p>}
        </div>
      )}
      {result.status === "cancelled" && (
        <div className="rounded bg-muted px-2 py-1.5 text-muted-foreground">
          <span className="font-medium">{t("workflows.cancelledTitle")}</span>
        </div>
      )}
      {(elapsedTotalS != null || typicalTotalS != null) && (
        <div className="flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
          {elapsedTotalS != null && (
            <span className="tabular-nums">
              {t("workflows.runElapsed")} {formatElapsed(0, elapsedTotalS * 1000)}
            </span>
          )}
          {typicalTotalS != null && (
            <span>
              {t("workflows.runTypicalTotal", { duration: formatElapsed(0, typicalTotalS * 1000) })}
            </span>
          )}
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        {result.runs.map((run, i) => (
          <RunNodeRow
            key={`${run.node_id}#${run.iteration}#${i}`}
            run={run}
            continuesSession={continues[i]}
            typicalS={result.typical_s?.[run.node_id]}
          />
        ))}
        {activeNodeInfo && (
          <div className="flex flex-wrap items-center gap-1.5 rounded border border-amber-500/50 bg-amber-500/5 px-2 py-1.5">
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-amber-600" aria-hidden />
            <span className="font-mono text-[11px] font-medium">{activeNodeInfo.label}</span>
            <span className="rounded bg-amber-500/10 px-1 py-0.5 text-[10px] text-amber-700 dark:text-amber-400">
              {t("workflows.runStatus.running", "running")}
            </span>
            <span className="ml-auto shrink-0 tabular-nums text-[10px] text-muted-foreground">
              {formatElapsed(activeNodeInfo.started_at * 1000, now)}
            </span>
          </div>
        )}
      </div>
      {result.status === "completed" && result.final_output && (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("workflows.finalOutput")}
            {result.final_output_node && (
              <> · {t("workflows.finalOutputFrom", { node: result.final_output_node })}</>
            )}
          </span>
          <div className="whitespace-pre-wrap break-words text-muted-foreground">
            {result.final_output}
          </div>
        </div>
      )}
      {result.output_dir && (
        <div className="font-mono text-[11px] text-muted-foreground">
          {t("workflows.outputDir")}: {result.output_dir}
        </div>
      )}
      {result.status === "completed" && outputFiles.length > 0 && (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("workflows.outputFiles")}
          </span>
          {outputFiles.slice(0, 20).map((f) => (
            <span key={f} className="flex items-center gap-1 font-mono text-[11px] text-muted-foreground">
              <FileIcon className="h-3 w-3 shrink-0" aria-hidden /> {f}
            </span>
          ))}
          {outputFiles.length > 20 && (
            <span className="text-[11px] text-muted-foreground">
              {t("workflows.andNMore", { count: outputFiles.length - 20 })}
            </span>
          )}
          <span className="text-[10px] text-muted-foreground opacity-70">
            {t("workflows.outputPruneHint")}
          </span>
        </div>
      )}
    </div>
  );
}
