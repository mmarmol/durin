import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DiffViewer } from "@/components/DiffViewer";
import { ApiError, type SkillSuggestion } from "@/lib/api";

/** Why accepting or rejecting a suggestion failed, in the reader's language.
 *  A machine-readable reason localizes; otherwise the server's own detail is
 *  shown, and the generic "already processed" guess only as a last resort. */
export function suggestionErrorMessage(
  err: unknown,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  if (err instanceof ApiError && err.details?.reason === "skill_quarantined") {
    return t("dream.bandeja.suggestionQuarantined", { skill: String(err.details.skill ?? "") });
  }
  const detail = err instanceof ApiError ? err.detail : undefined;
  return detail || t("dream.bandeja.suggestionError");
}

/** One curation suggestion for a manual skill: what it changes and why, the
 *  patch, and Accept / Reject. */
export function SkillSuggestionCard({
  suggestion,
  busy,
  onResolve,
}: {
  suggestion: SkillSuggestion;
  busy: boolean;
  onResolve: (action: "accept" | "reject") => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-border/40 bg-card px-4 py-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[12px] font-medium text-foreground">{suggestion.skill}</span>
        <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold text-amber-600 dark:text-amber-400">
          {t(`dream.bandeja.action.${suggestion.type}`)}
        </span>
      </div>
      <p className="text-[13px] text-muted-foreground">{suggestion.reason}</p>
      {suggestion.patch ? <DiffViewer patch={suggestion.patch} /> : null}
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="default"
          size="sm"
          className="text-[12px]"
          disabled={busy}
          onClick={() => onResolve("accept")}
        >
          {t("dream.bandeja.accept")}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="text-[12px]"
          disabled={busy}
          onClick={() => onResolve("reject")}
        >
          {t("dream.bandeja.reject")}
        </Button>
        <span className="ml-auto text-[11px] text-muted-foreground">
          {t("dream.bandeja.rejectHint")}
        </span>
      </div>
    </div>
  );
}
