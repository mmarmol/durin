// Regression test for the periodic "view reset" bug: the proactive bootstrap
// token refresh used to store the re-minted token in React state, so every
// rotation (80% of the TTL) changed the `token` prop flowing through
// ClientProvider and re-fired every `[token]`-keyed effect in every view —
// re-fetching lists, resetting selections, and discarding unsaved edits.
// The refresh must instead update the module-level token in lib/http and leave
// the React tree untouched.
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAllLoopRuns: vi.fn().mockResolvedValue([]),
    listAllWorkflowRuns: vi.fn().mockResolvedValue([]),
  };
});

vi.mock("@/hooks/useSessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useSessions")>();
  return {
    ...actual,
    useSessions: () => ({
      sessions: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      createChat: vi.fn(),
      deleteChat: vi.fn(),
      renameChat: vi.fn(),
    }),
  };
});

vi.mock("@/hooks/useTheme", () => ({
  PALETTES: ["ithildin", "forge", "mithril"] as const,
  useTheme: () => ({
    theme: "light" as const,
    toggle: vi.fn(),
    setTheme: () => {},
    palette: "ithildin" as const,
    setPalette: () => {},
  }),
}));

vi.mock("@/lib/bootstrap", () => ({
  // Each bootstrap mints a different token, like the real gateway does.
  fetchBootstrap: vi.fn(),
  deriveWsUrl: vi.fn(() => "ws://test"),
  signout: vi.fn(),
}));

vi.mock("@/lib/durin-client", () => {
  class MockClient {
    status = "idle" as const;
    defaultChatId: string | null = null;
    connect = vi.fn();
    onStatus = () => () => {};
    onRuntimeModelUpdate = () => () => {};
    onError = () => () => {};
    onChat = () => () => {};
    onVoiceState = () => () => {};
    onVoiceAudio = () => () => {};
    onConcurrencySnapshot = () => () => {};
    sendMessage = vi.fn();
    newChat = vi.fn();
    attach = vi.fn();
    close = vi.fn();
    updateUrl = vi.fn();
  }
  return { DurinClient: MockClient };
});

import App from "@/App";
import { listAllLoopRuns, listAllWorkflowRuns } from "@/lib/api";
import { fetchBootstrap } from "@/lib/bootstrap";
import { setCurrentToken } from "@/lib/http";

describe("token rotation", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    let mint = 0;
    (fetchBootstrap as unknown as ReturnType<typeof vi.fn>)
      .mockReset()
      .mockImplementation(async () => ({
        token: `tok-${++mint}`,
        ws_path: "/",
        // 10s TTL → proactive refresh fires at 8s, well before the 30s
        // badge-poll intervals, so those can't confound the call counts.
        expires_in: 10,
        requires_secret: false,
      }));
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404 }),
    );
  });

  afterEach(() => {
    setCurrentToken(null);
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("does not re-fire [token]-keyed view effects on proactive refresh", async () => {
    render(<App />);
    // Flush the initial bootstrap + mount effects.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchBootstrap).toHaveBeenCalledTimes(1);
    expect(listAllLoopRuns).toHaveBeenCalledTimes(1);
    expect(listAllWorkflowRuns).toHaveBeenCalledTimes(1);

    // Cross the proactive-refresh mark (80% of the 10s TTL = 8s).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8_500);
    });
    expect(fetchBootstrap).toHaveBeenCalledTimes(2);

    // The rotation must not have re-fired the [token]-keyed effects: the
    // badge polls only run again on their own 30s interval.
    expect(listAllLoopRuns).toHaveBeenCalledTimes(1);
    expect(listAllWorkflowRuns).toHaveBeenCalledTimes(1);
  });
});
