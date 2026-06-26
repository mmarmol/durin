// The single door every authenticated API call must go through. Keeping the
// `fetch` global confined to this module (enforced by no-raw-fetch.test.ts)
// guarantees a stale bootstrap token is always reauthed-and-retried instead of
// surfacing a 401 to the caller — the webui-thread-going-blank regression.

let reauthHandler: (() => Promise<string | null>) | null = null;

/** Register a callback that mints a fresh token. When a REST call gets
 *  a 401 — the gateway restarted and wiped its in-memory token pool, so
 *  the cached token is now stale — `fetchWithReauth` calls this, then retries
 *  once. Without it, every REST call stays broken until a page reload. */
export function setApiReauthHandler(
  handler: (() => Promise<string | null>) | null,
): void {
  reauthHandler = handler;
}

/** fetch with Bearer auth + one automatic retry on 401 (after the reauth
 *  handler mints a fresh token). Returns the raw Response so callers can read
 *  it however they need — `request` throws on non-2xx, while the skills
 *  endpoints parse their own 4xx problem+json envelopes. */
export async function fetchWithReauth(
  url: string,
  token: string,
  init?: RequestInit,
  retryOn401 = true,
): Promise<Response> {
  const res = await fetch(url, {
    ...(init ?? {}),
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
    credentials: "same-origin",
  });
  if (res.status === 401 && retryOn401 && reauthHandler) {
    const fresh = await reauthHandler();
    if (fresh && fresh !== token) {
      return fetchWithReauth(url, fresh, init, false);
    }
  }
  return res;
}
