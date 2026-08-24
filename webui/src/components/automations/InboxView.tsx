import { useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, answerAutomationRun, type AutomationRun } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

const quoteCls =
  "whitespace-pre-wrap rounded-md border-l-2 border-accent bg-muted/50 px-2.5 py-1.5 text-[12.5px]";

type Action = "approve" | "revise" | "reject" | "answer";

/** The full resolution card for one paused run (mockup screen 5), mounted
 *  wherever the caller has a currently-selected paused run — AutomationsView
 *  renders this right where NeedsYouTray's Revisar/Responder set the
 *  selection (see NeedsYouTray.tsx's own docstring for that hand-off; C2
 *  built the selection, this consumes it).
 *
 *  An approval shows the proposal in a quoted block and offers
 *  Aprobar/Corregir/Rechazar; a question is free-text answer + resume. Both
 *  call answerAutomationRun and, on success, hand a confirmation string up
 *  via onResolved — the caller is responsible for refreshing the runs list
 *  (which is what actually makes the run leave "paused") and clearing the
 *  selection, so this card has nothing further to do once that happens; it
 *  simply stops being rendered. */
export function InboxView({
  run,
  onResolved,
}: {
  run: AutomationRun;
  onResolved: (message: string) => void;
}) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState<Action | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isApproval = run.ask_kind === "approval";
  const commentEmpty = comment.trim() === "";
  // _park (durin/automations/runtime.py) sets `proposal = ask` for the
  // ordinary (non-counterpart) approval case — ask and proposal are the same
  // string there, so showing both separately would just print the identical
  // text twice. A counterpart-tagged approval always has `proposal: null`
  // regardless of ask_kind, so this falls back to ask for that shape instead
  // of rendering an empty quote. One body, sourced from whichever field
  // actually carries content.
  const proposalText = run.proposal ?? run.ask;

  async function submit(action: Action) {
    setSubmitting(action);
    setError(null);
    try {
      if (action === "answer") {
        await answerAutomationRun(token, run.automation, run.run_id, comment);
      } else {
        await answerAutomationRun(token, run.automation, run.run_id, action === "revise" ? comment : "", action);
      }
      onResolved(t(`automations.inbox.confirmed.${action}`));
    } catch (e) {
      setError(errMsg(e));
      setSubmitting(null);
    }
  }

  return (
    <div className="rounded-lg border border-border" data-testid="inbox-card">
      <div className="flex items-center gap-2 border-b border-border px-3.5 py-2.5 text-[13px] font-semibold">
        <span
          className={cn(
            "shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-medium",
            isApproval ? "bg-warn/15 text-warn" : "bg-accent/15 text-accent",
          )}
        >
          {t(isApproval ? "automations.tray.approval" : "automations.tray.question")}
        </span>
        <span className="min-w-0 truncate">{run.automation}</span>
        <span className="ml-auto shrink-0 text-[11px] font-normal text-muted-foreground">
          {relativeTime(run.started_at * 1000)}
        </span>
      </div>
      <div className="flex flex-col gap-2.5 px-3.5 py-3">
        {isApproval ? (
          <>
            {proposalText && <div className={quoteCls}>{proposalText}</div>}
            <Textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder={t("automations.inbox.reviseCommentPlaceholder")}
              className="min-h-[70px] text-[12.5px]"
            />
            {error && <p className="text-[12px] text-destructive">{error}</p>}
            <div className="flex flex-wrap gap-2">
              <Button size="sm" disabled={submitting !== null} onClick={() => void submit("approve")}>
                {submitting === "approve" ? <Loader2 className="h-4 w-4 animate-spin" /> : t("automations.inbox.approve")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={submitting !== null || commentEmpty}
                onClick={() => void submit("revise")}
              >
                {submitting === "revise" ? <Loader2 className="h-4 w-4 animate-spin" /> : t("automations.inbox.revise")}
              </Button>
              <Button
                size="sm"
                variant="destructive"
                disabled={submitting !== null}
                onClick={() => void submit("reject")}
              >
                {submitting === "reject" ? <Loader2 className="h-4 w-4 animate-spin" /> : t("automations.inbox.reject")}
              </Button>
            </div>
          </>
        ) : (
          <>
            <p className="text-[12.5px]">{run.ask}</p>
            <Textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder={t("automations.inbox.answerPlaceholder")}
              className="min-h-[70px] text-[12.5px]"
            />
            {error && <p className="text-[12px] text-destructive">{error}</p>}
            <div>
              <Button size="sm" disabled={submitting !== null || commentEmpty} onClick={() => void submit("answer")}>
                {submitting === "answer" ? <Loader2 className="h-4 w-4 animate-spin" /> : t("automations.inbox.answerAndResume")}
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
