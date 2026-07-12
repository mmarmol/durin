import { useCallback, useState, type KeyboardEvent } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { LoopRun } from "@/lib/api";
import { cn } from "@/lib/utils";

// Interactive controls shared by ActivityView's list rows and BoardView's
// cards, so the two presentations never fork how an answer gets sent or a
// run gets retried.

/** Accessibility props for an expandable row/card container: focusable and
 *  toggleable with Enter/Space. Deliberately no role="button" — the rows
 *  contain real buttons and inputs (nested interactive controls are invalid
 *  inside a button role, and the row's accessible name would swallow
 *  theirs). The keydown guard only reacts to keys on the container itself,
 *  so typing (or pressing Enter) inside an inner control like the answer
 *  input never collapses the row. */
export function expandableRowProps(onToggle: () => void) {
  return {
    tabIndex: 0,
    onClick: onToggle,
    onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onToggle();
      }
    },
  };
}

/** The answer text box + send button — used directly by needs_operator rows
 *  (list AnswerRow, board cards) and revealed by WaitingAnswerToggle for
 *  waiting_info rows/cards. */
export function AnswerInput({
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

  if (sent) {
    return <div className="text-xs text-muted-foreground">{t("loops.activity.answerSent")}</div>;
  }
  return (
    // Stops the click from bubbling to the row/card's onToggle — this input
    // sits inside containers that expand/collapse the run detail on body
    // click, and clicking into the answer box must not toggle that panel.
    <div className="flex gap-1.5" onClick={(e) => e.stopPropagation()}>
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
      <Button size="sm" disabled={answering || !answer.trim()} onClick={() => void handleSend()}>
        {answering ? <Loader2 className="h-4 w-4 animate-spin" /> : t("loops.activity.send")}
      </Button>
    </div>
  );
}

/** Reveals AnswerInput behind an "answer as operator" toggle — the ask on a
 *  waiting_info run is read-only by default (the counterpart answers, not
 *  the operator); this is the override. Used by the list's WaitingInfoRow
 *  and the board's waiting_info cards. */
export function WaitingAnswerToggle({
  run,
  onAnswer,
  answering,
}: {
  run: LoopRun;
  onAnswer: (run: LoopRun, answer: string) => Promise<boolean>;
  answering: boolean;
}) {
  const { t } = useTranslation();
  const [showAnswer, setShowAnswer] = useState(false);
  if (showAnswer) {
    return <AnswerInput run={run} onAnswer={onAnswer} answering={answering} />;
  }
  return (
    <Button
      size="sm"
      variant="ghost"
      className="h-6 w-fit gap-1 px-2 text-[11px] text-muted-foreground"
      onClick={(e) => {
        e.stopPropagation();
        setShowAnswer(true);
      }}
    >
      {t("loops.activity.answerAsOperator")}
    </Button>
  );
}

/** Retry button for an escalated run — used by the list's RunRow and the
 *  board's Attention-column cards. */
export function RetryButton({
  run,
  onRetry,
  retrying,
  className,
}: {
  run: LoopRun;
  onRetry: (run: LoopRun) => void;
  retrying: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <Button
      size="sm"
      variant="ghost"
      className={cn("h-6 gap-1 px-2 text-[11px]", className)}
      disabled={retrying}
      onClick={(e) => {
        e.stopPropagation();
        onRetry(run);
      }}
    >
      {retrying ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
      {t("loops.activity.retry")}
    </Button>
  );
}
