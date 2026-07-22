import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Ban,
  Check,
  Copy,
  FileIcon,
  HelpCircle,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { MarkdownText } from "@/components/MarkdownText";
import { Textarea } from "@/components/ui/textarea";
import type { WorkflowGlobalRun, WorkflowRunNode, WorkflowRunResult } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { formatElapsed, useTicker } from "@/lib/work-format";
import { cn } from "@/lib/utils";

/** Sum of every present value, or null if none are present — an absent sum reads as
 *  "no data" rather than a fabricated 0 (e.g. a workflow with no completed runs yet
 *  has no typical durations at all, not a typical of zero). */
function sumKnown(values: Array<number | null | undefined>): number | null {
  const known = values.filter((v): v is number => v != null);
  return known.length > 0 ? known.reduce((a, b) => a + b, 0) : null;
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

// Run-level status → icon, sharing the work panel's vocabulary (spinner while
// running, check when done, X on hard failure, help while paused for input) so
// the executions list and the chat's work surfaces read as one system.
export function RunStatusIcon({ status, className }: { status: string; className?: string }) {
  const cls = cn("h-3.5 w-3.5 shrink-0", className);
  if (status === "running") return <Loader2 className={cn(cls, "animate-spin text-amber-600")} aria-hidden />;
  if (status === "completed") return <Check className={cn(cls, "text-emerald-600")} aria-hidden />;
  if (status === "needs_input") return <HelpCircle className={cn(cls, "text-accent-foreground")} aria-hidden />;
  if (status === "exhausted") return <AlertTriangle className={cn(cls, "text-warn")} aria-hidden />;
  if (status === "aborted" || status === "crashed") return <X className={cn(cls, "text-destructive")} aria-hidden />;
  if (status === "cancelled") return <Ban className={cn(cls, "text-muted-foreground")} aria-hidden />;
  return (
    <span className={cn(cls, "flex items-center justify-center text-muted-foreground/60")} aria-hidden>
      ·
    </span>
  );
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

// Icon-only copy affordance for the run's final output (the payload someone most
// often wants to take elsewhere).
function CopyOutputButton({ value }: { value: string }) {
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
      aria-label={copied ? t("workflows.outputCopied") : t("workflows.copyOutput")}
      title={t("workflows.copyOutput")}
      className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
    >
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
    </button>
  );
}

// Node-level status: ok collapses to a check icon plus screen-reader text (a chip
// repeating "ok" on every healthy row is noise); anything else keeps a labelled
// chip because the words carry real diagnosis ("save failed" vs "failed" vs
// "no session").
function NodeStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  if (status === "ok") {
    return (
      <>
        <Check className="h-3 w-3 shrink-0 text-emerald-600" aria-hidden />
        <span className="sr-only">{t("workflows.runStatus.ok", "ok")}</span>
      </>
    );
  }
  const tone =
    status === "node_failed" || status === "persist_failed"
      ? "bg-destructive/10 text-destructive"
      : "bg-muted text-muted-foreground";
  return (
    <span className={cn("rounded px-1 py-0.5 text-[10px]", tone)}>
      {t("workflows.runStatus." + status, status)}
    </span>
  );
}

// One per-node/worker row in a run's trace: identity (node_id#iteration, or
// "pass N of budget" once the node has a known visit budget), fan-out
// worker_index / static branch_id so concurrent units are legible, a status
// badge and route verdict, a "continues session" chip when this row picks up
// an earlier row's session (a resumed/looping node), durations right-aligned
// (actual next to the node's typical), then the node's (truncated) output,
// produced artifacts and the copyable session key.
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
  const artifacts = run.artifacts ?? [];

  return (
    <div className="flex flex-col gap-1 border-b border-border/60 px-2.5 py-1.5 last:border-b-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <NodeStatusBadge status={run.status} />
        <span className="font-mono text-[11.5px] font-medium">
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
          <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
            {t("workflows.workerLabel", { index: run.worker_index })}
          </span>
        )}
        {run.branch_id && (
          <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
            {t("workflows.branchLabel", { id: run.branch_id })}
          </span>
        )}
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
        {metaParts.length > 0 && (
          <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground">
            {metaParts.join(" · ")}
          </span>
        )}
      </div>
      {run.output && (
        <div className="whitespace-pre-wrap break-words text-[11px] text-muted-foreground">
          {run.output}
        </div>
      )}
      {(artifacts.length > 0 || run.session_key) && (
        <div className="flex flex-wrap items-center gap-1">
          {artifacts.length > 0 && (
            <>
              <span className="text-[10px] text-muted-foreground">
                {t("workflows.nodeArtifacts")}:
              </span>
              {artifacts.map((a) => (
                <span
                  key={a}
                  className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                >
                  {a}
                </span>
              ))}
            </>
          )}
          {run.session_key && (
            <span className="ml-auto min-w-0">
              <CopyableKey value={run.session_key} />
            </span>
          )}
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
// cancelled), the run's node trace (status, output, session affordance, loop pass,
// durations), the resume form (needs_input only), nested sub-runs when the caller
// passes them, the final output rendered as markdown, output folder and files.
export function RunDetail({
  result,
  onResume,
  resuming,
  childRuns,
  onOpenRun,
}: {
  result: WorkflowRunResult;
  onResume: (answers: string) => void;
  resuming: boolean;
  // Runs this run spawned through subworkflow nodes (from the global feed), in
  // execution order; rendered as a navigable section when provided.
  childRuns?: WorkflowGlobalRun[];
  onOpenRun?: (run: WorkflowGlobalRun) => void;
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
    <div className="flex flex-col gap-3">
      {result.status === "needs_input" && (
        <div className="flex flex-col gap-1.5 rounded-md bg-accent px-3 py-2 text-accent-foreground">
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
        <div className="flex flex-col gap-0.5 rounded-md bg-warn/10 px-3 py-2 text-warn">
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
        <div className="flex flex-col gap-1 rounded-md bg-destructive/10 px-3 py-2 text-destructive">
          <div className="flex items-center gap-1.5">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="font-medium">{t("workflows.abortedTitle")}</span>
          </div>
          {result.final_output && <p className="whitespace-pre-wrap break-words">{result.final_output}</p>}
        </div>
      )}
      {result.status === "cancelled" && (
        <div className="rounded-md bg-muted px-3 py-2 text-muted-foreground">
          <span className="font-medium">{t("workflows.cancelledTitle")}</span>
        </div>
      )}
      {(elapsedTotalS != null || typicalTotalS != null) && (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
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
      {(result.runs.length > 0 || activeNodeInfo) && (
        <div className="overflow-hidden rounded-md border">
          {result.runs.map((run, i) => (
            <RunNodeRow
              key={`${run.node_id}#${run.iteration}#${i}`}
              run={run}
              continuesSession={continues[i]}
              typicalS={result.typical_s?.[run.node_id]}
            />
          ))}
          {activeNodeInfo && (
            <div className="flex flex-wrap items-center gap-1.5 bg-amber-500/5 px-2.5 py-1.5">
              <Loader2 className="h-3 w-3 shrink-0 animate-spin text-amber-600" aria-hidden />
              <span className="font-mono text-[11.5px] font-medium">{activeNodeInfo.label}</span>
              <span className="rounded bg-amber-500/10 px-1 py-0.5 text-[10px] text-amber-700 dark:text-amber-400">
                {t("workflows.runStatus.running", "running")}
              </span>
              <span className="ml-auto shrink-0 tabular-nums text-[10px] text-muted-foreground">
                {formatElapsed(activeNodeInfo.started_at * 1000, now)}
              </span>
            </div>
          )}
        </div>
      )}
      {childRuns != null && childRuns.length > 0 && onOpenRun != null && (
        <div className="flex flex-col gap-1">
          <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {t("runs.subRuns")} · {childRuns.length}
          </span>
          <div className="overflow-hidden rounded-md border">
            {childRuns.map((child) => (
              <button
                key={child.run_id}
                type="button"
                onClick={() => onOpenRun(child)}
                className="flex w-full items-center gap-2 border-b border-border/60 px-2.5 py-1.5 text-left last:border-b-0 hover:bg-muted/50"
              >
                <RunStatusIcon status={child.status} className="h-3 w-3" />
                <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]">{child.workflow}</span>
                <span className="sr-only">
                  {t("workflows.runStatus." + child.status, child.status)}
                </span>
                {!!child.started_at && (
                  <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                    {relativeTime(child.started_at * 1000)}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
      {result.status === "completed" && result.final_output && (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1.5">
            {/* One label, not two: "final output from X" already contains "final
                output", so appending it to the plain label read as a stutter. */}
            <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              {result.final_output_node
                ? t("workflows.finalOutputFrom", { node: result.final_output_node })
                : t("workflows.finalOutput")}
            </span>
            <CopyOutputButton value={result.final_output} />
          </div>
          <MarkdownText
            className={cn(
              "max-w-[74ch] text-[13px] leading-relaxed text-foreground/92",
              "prose-headings:mt-3 prose-headings:mb-1",
              "prose-h1:text-[16px] prose-h2:text-[14px] prose-h3:text-[13px] prose-h4:text-[13px]",
              "prose-p:my-1.5 prose-ul:my-1.5 prose-ol:my-1.5 prose-li:my-0.5",
            )}
          >
            {result.final_output}
          </MarkdownText>
        </div>
      )}
      {result.output_dir && (
        <div className="font-mono text-[11px] text-muted-foreground">
          {t("workflows.outputDir")}: {result.output_dir}
        </div>
      )}
      {result.status === "completed" && outputFiles.length > 0 && (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
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
