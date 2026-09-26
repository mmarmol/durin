import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Minimal App-mount scaffolding, mirroring app-layout.test.tsx. Kept in its
// own file (rather than added to that shared one) because this test replaces
// SkillsView and ThreadShell with stubs, which the other App-layout tests
// must not be affected by.

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAllAutomationRuns: vi.fn().mockResolvedValue([]),
    listPending: vi.fn().mockResolvedValue({ items: [], count: 0, errors: [] }),
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
      createChat: vi.fn().mockResolvedValue("chat-1"),
      deleteChat: vi.fn(),
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

const connectSpy = vi.fn();

vi.mock("@/lib/bootstrap", () => ({
  fetchBootstrap: vi.fn().mockResolvedValue({
    token: "tok",
    ws_path: "/",
    expires_in: 300,
    requires_secret: false,
  }),
  deriveWsUrl: vi.fn(() => "ws://test"),
  signout: vi.fn(),
}));

vi.mock("@/lib/durin-client", () => {
  class MockClient {
    status = "idle" as const;
    defaultChatId: string | null = null;
    connect = connectSpy;
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

vi.mock("@/components/SkillsView", () => ({
  SkillsView: ({ onAskDurin }: { onAskDurin?: (binName: string) => void }) => (
    <button type="button" onClick={() => onAskDurin?.("docker")}>
      trigger ask durin
    </button>
  ),
}));

// The fix under test is entirely in App.tsx's own onAskDurin callback (what
// prompt string it builds), not in how the real composer later renders that
// prompt. Reading the prop straight off a stub avoids the real shell's
// session/composer-variant machinery, which needs an active chat session to
// even accept a pendingPrompt.
vi.mock("@/components/thread/ThreadShell", () => ({
  ThreadShell: ({ pendingPrompt }: { pendingPrompt?: string | null }) => (
    <div data-testid="pending-prompt">{pendingPrompt}</div>
  ),
}));

import App from "@/App";

describe("App asks durin to install a missing binary", () => {
  it("builds the prompt from i18n, interpolating the binary name", async () => {
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Skills" }));
    fireEvent.click(await screen.findByText("trigger ask durin"));

    expect(await screen.findByTestId("pending-prompt")).toHaveTextContent(
      "Help me install docker",
    );
  });
});
