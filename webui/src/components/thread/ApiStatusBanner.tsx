import { Loader2, AlertTriangle, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ApiRetryStatus } from "@/lib/types";

interface ApiStatusBannerProps {
  status: ApiRetryStatus;
  onDismiss: () => void;
}

/**
 * Surfaces an in-flight provider retry as a transient banner above the
 * composer. ``final=true`` flips the banner to a destructive variant
 * (retries exhausted; the next assistant turn carries the error). The
 * banner auto-clears on ``turn_end`` from useDurinStream.
 */
export function ApiStatusBanner({ status, onDismiss }: ApiStatusBannerProps) {
  const { t } = useTranslation();
  const isFinal = status.final;
  const Icon = isFinal ? AlertTriangle : Loader2;
  const tone = isFinal ? "destructive" : "muted";

  const title = resolveTitle(status, t);
  const body = resolveBody(status, t);

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "mb-2 flex items-start gap-2 rounded-lg border px-3 py-2 text-[12px] leading-5",
        "animate-in fade-in-0 slide-in-from-bottom-1",
        tone === "destructive"
          ? "border-destructive/30 bg-destructive/10 text-destructive"
          : "border-muted-foreground/20 bg-muted/40 text-muted-foreground",
      )}
    >
      <Icon
        className={cn(
          "mt-0.5 h-4 w-4 shrink-0",
          !isFinal && "animate-spin",
        )}
        aria-hidden
      />
      <div className="flex-1">
        <p className="font-medium">{title}</p>
        {body && <p className="mt-0.5 opacity-80">{body}</p>}
      </div>
      <Button
        variant="ghost"
        size="icon"
        onClick={onDismiss}
        aria-label={t("common.dismiss")}
        className={cn(
          "h-6 w-6 shrink-0",
          tone === "destructive"
            ? "text-destructive hover:bg-destructive/15 hover:text-destructive"
            : "text-muted-foreground hover:bg-muted/60",
        )}
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}

function resolveTitle(status: ApiRetryStatus, t: TFunction): string {
  if (status.kind === "giving_up") {
    return t("apiStatus.givingUpTitle", { attempt: status.attempt });
  }
  if (status.kind === "exhausted_persistent") {
    return t("apiStatus.exhaustedTitle");
  }
  const attemptLabel = status.max_attempts
    ? t("apiStatus.attemptOf", { attempt: status.attempt, max: status.max_attempts })
    : t("apiStatus.attempt", { attempt: status.attempt });
  if (status.delay_s > 0) {
    return t("apiStatus.retryingIn", { delay: status.delay_s, attemptLabel });
  }
  return t("apiStatus.retrying", { attemptLabel });
}

function resolveBody(status: ApiRetryStatus, t: TFunction): string | null {
  if (status.kind === "giving_up") {
    return t("apiStatus.givingUpBody");
  }
  if (status.kind === "exhausted_persistent") {
    return t("apiStatus.exhaustedBody");
  }
  return t("apiStatus.transientBody");
}
