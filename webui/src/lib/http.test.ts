import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchWithReauth, setApiReauthHandler, setCurrentToken } from "./http";

function lastAuthHeader(fetchMock: ReturnType<typeof vi.fn>): string {
  const call = fetchMock.mock.calls.at(-1);
  const init = call?.[1] as RequestInit;
  return (init.headers as Record<string, string>).Authorization;
}

describe("fetchWithReauth current-token override", () => {
  afterEach(() => {
    setCurrentToken(null);
    setApiReauthHandler(null);
    vi.unstubAllGlobals();
  });

  it("prefers the module-level current token over the caller's stale token", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    vi.stubGlobal("fetch", fetchMock);

    setCurrentToken("fresh-token");
    await fetchWithReauth("/api/v1/thing", "stale-token");

    expect(lastAuthHeader(fetchMock)).toBe("Bearer fresh-token");
  });

  it("falls back to the caller's token when no current token is set", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    vi.stubGlobal("fetch", fetchMock);

    await fetchWithReauth("/api/v1/thing", "caller-token");

    expect(lastAuthHeader(fetchMock)).toBe("Bearer caller-token");
  });

  it("still reauths-and-retries on 401 against the current token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 401 })
      .mockResolvedValueOnce({ ok: true, status: 200 });
    vi.stubGlobal("fetch", fetchMock);
    setCurrentToken("expired-token");
    setApiReauthHandler(async () => "minted-token");

    const res = await fetchWithReauth("/api/v1/thing", "stale-token");

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(lastAuthHeader(fetchMock)).toBe("Bearer minted-token");
  });
});
