import { useEffect } from "react";

/**
 * Proactively re-mint the bootstrap token before it expires.
 *
 * Without this the cached token simply expires every TTL, and the next request
 * (the 4s task poll, a settings fetch, …) 401s before the reactive handler
 * re-bootstraps — recoverable, but it spams the console on a fixed cycle.
 * Refreshing ahead of expiry keeps the token continuously valid so no request
 * ever sees a 401.
 *
 * @param enabled        only run while the app holds a live session
 * @param expiresInSec   token TTL reported by /webui/bootstrap (0 = unknown)
 * @param refresh        re-mint + store a fresh token; must be stable (useCallback)
 */
export function useTokenRefresh(
  enabled: boolean,
  expiresInSec: number,
  refresh: () => void,
): void {
  useEffect(() => {
    if (!enabled || expiresInSec <= 0) return;
    // Refresh at 80% of the TTL — well before expiry, with headroom for clock
    // skew and request latency. Floor at 5s so a tiny TTL can't busy-loop.
    const delayMs = Math.max(5_000, expiresInSec * 1000 * 0.8);
    const id = setInterval(refresh, delayMs);
    return () => clearInterval(id);
  }, [enabled, expiresInSec, refresh]);
}
