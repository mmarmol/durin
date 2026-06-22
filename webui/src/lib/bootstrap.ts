import type { BootstrapResponse } from "./types";

/**
 * Fetch a short-lived token + the WebSocket path from the gateway's
 * ``/webui/bootstrap`` endpoint.
 *
 * Authentication is session-cookie based: pass the setup ``secret`` only on the
 * initial sign-in. On success the gateway sets an ``httpOnly`` ``durin_session``
 * cookie; subsequent calls (reloads, token refresh) send no secret and are
 * re-authorized by that cookie, which the browser attaches automatically
 * (``credentials: "same-origin"``). The secret is never stored client-side.
 */
export async function fetchBootstrap(
  baseUrl: string = "",
  secret: string = "",
): Promise<BootstrapResponse> {
  const headers: Record<string, string> = {};
  if (secret) {
    headers["X-Durin-Auth"] = secret;
  }
  const res = await fetch(`${baseUrl}/webui/bootstrap`, {
    method: "GET",
    credentials: "same-origin",
    headers,
  });
  if (!res.ok) {
    throw new Error(`bootstrap failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as BootstrapResponse;
  if (!body.token || !body.ws_path) {
    throw new Error("bootstrap response missing token or ws_path");
  }
  return body;
}

/**
 * End the session: ask the gateway to revoke the session token and clear the
 * ``durin_session`` cookie. Best-effort — network errors are ignored because
 * the caller transitions to the auth screen regardless (the cookie also
 * expires on its own).
 */
export async function signout(baseUrl: string = ""): Promise<void> {
  try {
    await fetch(`${baseUrl}/webui/signout`, {
      method: "POST",
      credentials: "same-origin",
    });
  } catch {
    // ignore — the UI re-prompts and the cookie expires server-side.
  }
}

/** Derive a WebSocket URL from the current window location and the server-provided path.
 *
 * Keeps the path segment exactly as the server registered it: the root ``/``
 * stays ``/`` and non-root paths are not given an extra trailing slash. This
 * matters because some WS servers dispatch handshakes based on the literal
 * path, not a normalised form.
 */
export function deriveWsUrl(wsPath: string, token: string): string {
  const path = wsPath && wsPath.startsWith("/") ? wsPath : `/${wsPath || ""}`;
  const query = `?token=${encodeURIComponent(token)}`;
  if (typeof window === "undefined") {
    return `ws://127.0.0.1:8765${path}${query}`;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host;
  return `${scheme}://${host}${path}${query}`;
}
