import { useEffect, useState } from "react";
import { HelpCircle, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

/** The resume form of a workflow run waiting for input: why it paused, the
 *  questions it asked, and the answers that resume this same run at the node
 *  that paused (same working folder and sessions). A run written before the
 *  resume feature has no node to re-enter, so it shows the questions only. */
export function NeedsInputForm({
  runId,
  needsInputNode,
  questions,
  resuming,
  onResume,
}: {
  runId: string;
  needsInputNode: string | null;
  questions: string;
  resuming: boolean;
  onResume: (answers: string) => void;
}) {
  const { t } = useTranslation();
  const [answers, setAnswers] = useState("");

  // A new run in the same form (a nested resume re-renders it with another
  // run) starts with empty answers, never the previous run's text.
  useEffect(() => {
    setAnswers("");
  }, [runId]);

  return (
    <div className="flex flex-col gap-1.5 rounded-md bg-accent px-3 py-2 text-accent-foreground">
      <div className="flex items-center gap-1.5">
        <HelpCircle className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span className="font-medium">{t("workflows.needsInputTitle")}</span>
      </div>
      <p>
        {t("workflows.needsInputBody", { node: needsInputNode || "?" })}
      </p>
      {questions && (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide opacity-70">
            {t("workflows.questionsFromRun")}
          </span>
          <div className="whitespace-pre-wrap break-words">{questions}</div>
        </div>
      )}
      {needsInputNode && (
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
            {t("workflows.resumeCaption", { node: needsInputNode, runId })}
          </span>
        </>
      )}
    </div>
  );
}
