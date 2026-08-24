import { useEffect, useState } from "react";
import { FileIcon, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { causeIcon, outcomeChipCls } from "@/components/automations/RunHistory";
import { MarkdownText } from "@/components/MarkdownText";
import { Button } from "@/components/ui/button";
import { getWorkflowRunManifest, type AutomationRun, type AutomationSummary, type WorkflowRunResult } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";
import { formatElapsed } from "@/lib/work-format";
import { useClient } from "@/providers/ClientProvider";

const chipMonoCls =
  "inline-flex items-center rounded-full border border-border bg-muted/60 px-2 py-0.5 font-mono text-[11px] text-muted-foreground";

const dtCls = "text-[11.5px] text-muted-foreground";
const ddCls = "text-[12.5px]";

/** The selected history run's full detail: cause (quoted), outcome, delivery
 *  and approval records, and — from the workflow manifest — the final
 *  output, files and working folder. Mockup screen 4's right pane. */
export function RunDetailCard({
  run,
  automation,
  onOpenWorkflowRun,
}: {
  run: AutomationRun;
  automation: AutomationSummary;
  onOpenWorkflowRun: (workflow: string, runId: string) => void;
}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [manifest, setManifest] = useState<WorkflowRunResult | null>(null);
  const [manifestLoading, setManifestLoading] = useState(false);

  const runId = run.workflow_run_id;

  useEffect(() => {
    setManifest(null);
    if (!runId) return;
    let cancelled = false;
    setManifestLoading(true);
    getWorkflowRunManifest(token, automation.workflow, runId)
      .then((m) => {
        if (!cancelled) setManifest(m);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setManifestLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, automation.workflow, runId]);

  const durationS =
    run.finished_at != null && run.started_at != null ? run.finished_at - run.started_at : null;

  const approvalText = run.approval
    ? t(`automations.detail.approvalAction.${run.approval.action}`, {
        by: run.approval.by,
        defaultValue: `${run.approval.action} · ${run.approval.by}`,
      })
    : null;

  const outputFiles = manifest?.output_files ?? [];

  return (
    <div className="rounded-lg border border-border" data-testid="run-detail-card">
      <div className="flex items-center justify-between border-b border-border px-3.5 py-2.5 text-[13px] font-semibold">
        <span className="min-w-0 truncate">
          {t("automations.detail.runLabel")} <span className="font-mono text-[11px] text-muted-foreground">{run.run_id}</span>
        </span>
      </div>
      <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-2 px-3.5 py-3">
        <dt className={dtCls}>{t("automations.detail.causeLabel")}</dt>
        <dd className={ddCls}>
          <div className="text-[11.5px] text-muted-foreground">
            {causeIcon(run.cause.kind)} · {fmtDateTime(run.started_at * 1000)}
          </div>
          <div className="mt-1 rounded-md border-l-2 border-accent bg-muted/50 px-2.5 py-1.5 text-[12.5px]">
            {run.cause.excerpt}
          </div>
        </dd>

        <dt className={dtCls}>{t("automations.detail.outcomeLabel")}</dt>
        <dd className={ddCls}>
          <span className={`rounded-full px-2 py-0.5 text-[10.5px] font-medium ${outcomeChipCls(run.status)}`}>
            {t(`automations.list.runStatus.${run.status}`, run.status)}
          </span>
          {durationS != null && (
            <span className="ml-2 text-[11.5px] text-muted-foreground">{formatElapsed(0, durationS * 1000)}</span>
          )}
        </dd>

        {run.delivery && (
          <>
            <dt className={dtCls}>{t("automations.detail.deliveryLabel")}</dt>
            <dd className={ddCls}>
              → {run.delivery.channel}
              {run.delivery.to ? ` · ${run.delivery.to}` : ""} · {fmtDateTime(run.delivery.at_ms)} ·{" "}
              {t(`automations.detail.deliveryResult.${run.delivery.result}`, run.delivery.result)}
            </dd>
          </>
        )}

        {run.approval && (
          <>
            <dt className={dtCls}>{t("automations.detail.approvalLabel")}</dt>
            <dd className={ddCls}>
              {approvalText} · {fmtDateTime(run.approval.at_ms)}
            </dd>
          </>
        )}

        {manifestLoading && (
          <>
            <dt />
            <dd className="flex items-center gap-2 text-[11.5px] text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> {t("workflows.loading")}
            </dd>
          </>
        )}

        {manifest?.final_output && (
          <>
            <dt className={dtCls}>{t("automations.detail.resultLabel")}</dt>
            <dd className={ddCls}>
              <div className="rounded-md border-l-2 border-accent bg-muted/50 px-2.5 py-1.5">
                <MarkdownText className="text-[12.5px] leading-relaxed text-foreground/92">
                  {manifest.final_output}
                </MarkdownText>
              </div>
            </dd>
          </>
        )}

        {manifest && (outputFiles.length > 0 || manifest.output_dir) && (
          <>
            <dt className={dtCls}>{t("automations.detail.filesLabel")}</dt>
            <dd className={`${ddCls} flex flex-wrap items-center gap-1.5`}>
              {outputFiles.slice(0, 20).map((f) => (
                <span key={f} className={chipMonoCls}>
                  <FileIcon className="mr-1 h-3 w-3" aria-hidden /> {f}
                </span>
              ))}
              {manifest.output_dir && (
                <span className="text-[11px] text-muted-foreground">
                  {t("workflows.outputDir")}: <span className="font-mono">{manifest.output_dir}</span>
                </span>
              )}
            </dd>
          </>
        )}
      </dl>
      {runId && (
        <div className="flex justify-end border-t border-border px-3.5 py-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenWorkflowRun(automation.workflow, runId)}>
            {t("automations.detail.openRun")}
          </Button>
        </div>
      )}
    </div>
  );
}
