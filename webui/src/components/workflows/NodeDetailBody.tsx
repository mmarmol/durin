import { useTranslation } from "react-i18next";

import { ThreadMessages } from "@/components/thread/ThreadMessages";
import { NodeActivityLine } from "@/components/work/NodeActivityLine";
import { CopyableKey } from "@/components/workflows/RunDetail";
import type { NodeTranscriptState } from "@/hooks/useNodeTranscript";
import type { WorkflowRunNode } from "@/lib/api";
import type { UIMessage, WorkActivity } from "@/lib/types";
import { formatElapsed } from "@/lib/work-format";

export interface NodeDetailBodyProps {
  row: WorkflowRunNode;
  /** Median seconds this node takes, from the run's recorded baseline. */
  typicalS?: number;
  /** What the node is doing right now; absent once it has finished. */
  activity?: WorkActivity | null;
  round?: number | null;
  maxRounds?: number | null;
  transcript: UIMessage[] | null;
  transcriptState: NodeTranscriptState;
}

/** One labelled block of captured script output. Renders nothing for an empty
 *  stream: an empty labelled box is a thing the reader has to parse before
 *  discovering it says nothing. */
function StreamBlock({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}) {
  if (!value.trim()) return null;
  return (
    <div className="flex flex-col gap-1" data-testid={testId}>
      <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted px-2.5 py-2 font-mono text-[11px] text-foreground/90">
        {value}
      </pre>
    </div>
  );
}

/**
 * The body of the node panel: the facts of one pass, then whatever record that
 * node kind keeps — an agent node's conversation, a script node's command and
 * streams, or nothing at all for a row whose work lives in its children.
 *
 * Purely presentational. The container decides what to fetch and when to poll,
 * so every branch here is reachable from a fixture.
 */
export function NodeDetailBody({
  row,
  typicalS,
  activity,
  round,
  maxRounds,
  transcript,
  transcriptState,
}: NodeDetailBodyProps) {
  const { t } = useTranslation();
  // Which record this node kept, decided by what the row actually carries rather
  // than by a declared kind: the trace records no node kind, and inventing one
  // here would be a second source of truth that could disagree with the row.
  const isScript = row.command != null || row.exit_code != null;
  const hasSession = !!row.session_key;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        <span className="font-mono text-[12px] font-medium text-foreground">{row.node_id}</span>
        {row.budget != null && (
          <span>{t("workflows.passOf", { iteration: row.iteration, budget: row.budget })}</span>
        )}
        {row.duration_s != null && (
          <span className="tabular-nums">
            {t("workflows.nodeDuration")} {formatElapsed(0, row.duration_s * 1000)}
          </span>
        )}
        {typicalS != null && (
          <span className="tabular-nums">
            {t("workflows.nodeTypical")} {formatElapsed(0, typicalS * 1000)}
          </span>
        )}
        {row.session_key && (
          <span className="ml-auto min-w-0">
            <CopyableKey value={row.session_key} />
          </span>
        )}
      </div>

      {activity && (
        <div className="flex flex-col gap-0.5 rounded-md bg-amber-500/5 px-2.5 py-1.5">
          <NodeActivityLine activity={activity} />
          {round != null && (
            <span
              className="text-[10px] tabular-nums text-muted-foreground"
              data-testid="node-round"
            >
              {maxRounds != null
                ? t("workflows.nodePanel.round", { round, maxRounds })
                : t("workflows.nodePanel.roundNoMax", { round })}
            </span>
          )}
        </div>
      )}

      {isScript && (
        <div className="flex flex-col gap-2">
          {row.command && (
            <div className="flex flex-col gap-1">
              <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                {t("workflows.nodePanel.command")}
              </span>
              <code className="break-all rounded bg-muted px-2 py-1 font-mono text-[11px]">
                {row.command}
              </code>
            </div>
          )}
          {row.exit_code != null && (
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <span>{t("workflows.nodePanel.exitCode")}</span>
              <span className="font-mono text-foreground">{row.exit_code}</span>
            </div>
          )}
          <StreamBlock
            label={t("workflows.nodePanel.stdout")}
            value={row.stdout ?? ""}
            testId="node-stdout"
          />
          <StreamBlock
            label={t("workflows.nodePanel.stderr")}
            value={row.stderr ?? ""}
            testId="node-stderr"
          />
        </div>
      )}

      {hasSession && (
        <div className="flex flex-col gap-1">
          <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {t("workflows.nodePanel.transcript")}
          </span>
          {transcriptState === "loading" && (
            <span
              className="text-[11px] text-muted-foreground"
              data-testid="node-transcript-loading"
            >
              {t("workflows.nodePanel.transcriptLoading")}
            </span>
          )}
          {transcriptState === "missing" && (
            <span
              className="text-[11px] text-muted-foreground"
              data-testid="node-transcript-missing"
            >
              {t("workflows.nodePanel.transcriptMissing")}
            </span>
          )}
          {transcript != null && transcript.length > 0 && (
            <ThreadMessages messages={transcript} />
          )}
        </div>
      )}

      {!isScript && !hasSession && (
        <p className="text-[11px] text-muted-foreground" data-testid="node-no-detail">
          {t("workflows.nodePanel.noDetail")}
        </p>
      )}
    </div>
  );
}
