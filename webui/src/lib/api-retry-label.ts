import type { TFunction } from "i18next";
import type { ApiRetryStatus } from "@/lib/types";

/** Build the short status title for a provider retry event. Used in the
 * run-strip row inside the composer. */
export function resolveTitle(status: ApiRetryStatus, t: TFunction): string {
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

