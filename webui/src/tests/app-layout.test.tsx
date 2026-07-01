import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatSummary, InboundEvent } from "@/lib/types";

type ConcurrencySnapshotEvent = Extract<InboundEvent, { event: "concurrency_snapshot" }>;

const connectSpy = vi.fn();
// Multiple components (the sidebar's chip, the global Work panel) each
// subscribe independently, so the mock must broadcast to every subscriber
// rather than remembering only the most recent one.
const concurrencySnapshotHandlers = new Set<(ev: ConcurrencySnapshotEvent) => void>();
function emitConcurrencySnapshot(ev: ConcurrencySnapshotEvent) {
  concurrencySnapshotHandlers.forEach((handler) => handler(ev));
}
const refreshSpy = vi.fn();
const createChatSpy = vi.fn().mockResolvedValue("chat-1");
const deleteChatSpy = vi.fn();
const toggleThemeSpy = vi.fn();
let mockSessions: ChatSummary[] = [];

vi.mock("@/hooks/useSessions", async (importOriginal) => {
  const React = await import("react");
  const actual = await importOriginal<typeof import("@/hooks/useSessions")>();
  return {
    ...actual,
    useSessions: () => {
      const [sessions, setSessions] = React.useState(mockSessions);
      return {
        sessions,
        loading: false,
        error: null,
        refresh: refreshSpy,
        createChat: createChatSpy,
        deleteChat: async (key: string) => {
          await deleteChatSpy(key);
          setSessions((prev: ChatSummary[]) => prev.filter((s) => s.key !== key));
        },
      };
    },
  };
});

vi.mock("@/hooks/useTheme", () => ({
  PALETTES: ["ithildin", "forge", "mithril"] as const,
  useTheme: () => ({
    theme: "light" as const,
    toggle: toggleThemeSpy,
    setTheme: () => {},
    palette: "ithildin" as const,
    setPalette: () => {},
  }),
}));

vi.mock("@/lib/bootstrap", () => ({
  fetchBootstrap: vi.fn().mockResolvedValue({
    token: "tok",
    ws_path: "/",
    expires_in: 300,
    // Simulate a secret-gated deploy so the Logout button is rendered.
    // The "opens the settings view" test asserts on it.
    requires_secret: true,
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
    onConcurrencySnapshot = (handler: (ev: ConcurrencySnapshotEvent) => void) => {
      concurrencySnapshotHandlers.add(handler);
      return () => {
        concurrencySnapshotHandlers.delete(handler);
      };
    };
    sendMessage = vi.fn();
    newChat = vi.fn();
    attach = vi.fn();
    close = vi.fn();
    updateUrl = vi.fn();
  }

  return { DurinClient: MockClient };
});

import App from "@/App";

describe("App layout", () => {
  beforeEach(() => {
    mockSessions = [];
    connectSpy.mockClear();
    refreshSpy.mockReset();
    createChatSpy.mockClear();
    deleteChatSpy.mockReset();
    toggleThemeSpy.mockReset();
    concurrencySnapshotHandlers.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
      }),
    );
  });

  it("keeps sidebar layout out of the main thread width contract", async () => {
    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    const main = container.querySelector("main");
    expect(main).toBeInTheDocument();
    expect(main).not.toHaveAttribute("style");

    const asideClassNames = Array.from(container.querySelectorAll("aside")).map(
      (el) => el.className,
    );
    expect(asideClassNames.some((cls) => cls.includes("lg:block"))).toBe(true);
  });

  it("switches to the next session when deleting the active chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^First chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(screen.getByLabelText("Chat actions for First chat"), {
      button: 0,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));

    await waitFor(() =>
      expect(screen.getByText("Delete this chat?")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(deleteChatSpy).toHaveBeenCalledWith("websocket:chat-a"),
    );
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Second chat$/ }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Delete this chat?")).not.toBeInTheDocument();
    expect(document.body.style.pointerEvents).not.toBe("none");
  }, 15_000);

  it("opens the settings view from the sidebar footer", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/v1/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "openai/gpt-4o",
                provider: "auto",
                resolved_provider: "openai",
                has_api_key: true,
              },
              providers: [
                {
                  name: "openai",
                  label: "OpenAI",
                  configured: true,
                  api_key_hint: "open••••-key",
                },
                {
                  name: "openrouter",
                  label: "OpenRouter",
                  configured: false,
                  default_api_base: "https://openrouter.ai/api/v1",
                },
              ],
              web_search: {
                provider: "brave",
                api_key_hint: "BSAo••••ew20",
                base_url: null,
                providers: [
                  { name: "duckduckgo", label: "DuckDuckGo", credential: "none" },
                  { name: "brave", label: "Brave Search", credential: "api_key" },
                  { name: "tavily", label: "Tavily", credential: "api_key" },
                ],
              },
              runtime: {
                config_path: "/tmp/config.json",
              },
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "Settings" }));

    expect(await screen.findByRole("heading", { name: "General" })).toBeInTheDocument();
    expect(document.title).toBe("Settings · durin");
    expect(screen.queryByRole("navigation", { name: "Sidebar navigation" })).not.toBeInTheDocument();
    const settingsNav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(settingsNav).getByRole("button", { name: "General" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      within(settingsNav).getByRole("button", { name: "Model providers" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.getByDisplayValue("openai/gpt-4o")).toBeInTheDocument();

    // Model providers section now unifies connection + models per provider.
    fireEvent.click(within(settingsNav).getByRole("button", { name: "Model providers" }));
    expect(screen.getByText("OpenAI")).toBeInTheDocument();
    expect(screen.getByText("OpenRouter")).toBeInTheDocument();
    expect(screen.getByText("Connect")).toBeInTheDocument();
    // expanding a configured provider reveals its connection (masked key) inline
    fireEvent.click(screen.getByText("OpenAI"));
    expect(await screen.findByText("open••••-key")).toBeInTheDocument();

    // Web search is its own section.
    fireEvent.click(within(settingsNav).getByRole("button", { name: "Web search" }));
    expect(screen.getByText("Search provider")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Brave Search/ })).toBeInTheDocument();
    expect(screen.getByText("BSAo••••ew20")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByPlaceholderText("Leave blank to keep the current key"), {
      target: { value: "unsaved-brave-key" },
    });
    fireEvent.pointerDown(screen.getByRole("button", { name: /Brave Search/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Tavily" }));
    fireEvent.pointerDown(screen.getByRole("button", { name: /Tavily/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Brave Search" }));
    expect(screen.getByText("BSAo••••ew20")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("unsaved-brave-key")).not.toBeInTheDocument();
  });

  it("toggles the global Work panel from the saturation chip, and reaches Concurrency settings once via its gear without sticking on repeat opens", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/v1/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "openai/gpt-4o",
                provider: "auto",
                resolved_provider: "openai",
                has_api_key: true,
              },
              providers: [{ name: "openai", label: "OpenAI", configured: true }],
              web_search: {
                provider: "duckduckgo",
                api_key_hint: null,
                base_url: null,
                providers: [
                  { name: "duckduckgo", label: "DuckDuckGo", credential: "none" },
                ],
              },
              runtime: { config_path: "/tmp/config.json" },
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    // Feed a concurrency snapshot so the chip renders (it's hidden until the
    // first frame arrives).
    await waitFor(() => expect(concurrencySnapshotHandlers.size).toBeGreaterThan(0));
    emitConcurrencySnapshot({
      event: "concurrency_snapshot",
      lanes: {
        interactive: { active: 1, limit: 4 },
        ceiling: { active: 5, limit: 12 },
        subagents: { active: 1, limit: 3 },
      },
      queued: 0,
      work: [],
    });

    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const chip = await within(sidebar).findByRole("button", {
      name: "Concurrency: 5 of 12 slots in use",
    });

    // First click: opens the global Work panel (not Settings).
    fireEvent.click(chip);
    expect(await screen.findByRole("complementary", { name: "Activity — all sessions" })).toBeInTheDocument();

    // Second click: closes the panel again.
    fireEvent.click(chip);
    expect(screen.queryByRole("complementary", { name: "Activity — all sessions" })).not.toBeInTheDocument();

    // Reopen, then use the panel's gear to deep-link into Settings.
    fireEvent.click(chip);
    fireEvent.click(screen.getByRole("button", { name: "Concurrency" }));
    expect(await screen.findByRole("heading", { name: "Concurrency" })).toBeInTheDocument();

    // Manually navigate to another settings tab, then back to chat.
    const settingsNav = screen.getByRole("navigation", { name: "Settings sections" });
    fireEvent.click(within(settingsNav).getByRole("button", { name: "General" }));
    expect(await screen.findByRole("heading", { name: "General" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));

    // Reopening Settings via the normal sidebar button must NOT stick on
    // Concurrency from the earlier gear click.
    const sidebarAgain = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebarAgain).getByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "General" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Concurrency" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));

    // A repeat chip → gear click still works after the manual navigation
    // above — proof the deep-link value is a one-shot, not permanently
    // consumed. The sidebar (and its snapshot subscription) remounted on
    // return to chat, so feed it a fresh snapshot before looking for the
    // chip again.
    await waitFor(() => expect(concurrencySnapshotHandlers.size).toBeGreaterThan(0));
    emitConcurrencySnapshot({
      event: "concurrency_snapshot",
      lanes: {
        interactive: { active: 1, limit: 4 },
        ceiling: { active: 5, limit: 12 },
        subagents: { active: 1, limit: 3 },
      },
      queued: 0,
      work: [],
    });
    const sidebarThird = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const chipAgain = await within(sidebarThird).findByRole("button", {
      name: "Concurrency: 5 of 12 slots in use",
    });
    // The panel was left open from the earlier interaction, so the first
    // click here closes it; a second click reopens it.
    fireEvent.click(chipAgain);
    fireEvent.click(chipAgain);
    fireEvent.click(screen.getByRole("button", { name: "Concurrency" }));
    expect(await screen.findByRole("heading", { name: "Concurrency" })).toBeInTheDocument();
  });

  it("hides the Sign out button when the deploy does not require a secret", async () => {
    // Override the default mock (which sets requires_secret: true for
    // the "opens the settings view" test) to simulate localhost-only
    // mode where the gateway auto-mints tokens. Logout would just
    // strand the user on an auth form they can't fill, so the
    // affordance must NOT render.
    const bootstrap = await import("@/lib/bootstrap");
    (bootstrap.fetchBootstrap as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({
        token: "tok",
        ws_path: "/",
        expires_in: 300,
        requires_secret: false,
      });

    render(<App />);
    const newChat = await screen.findByRole("button", { name: "New chat" });
    fireEvent.click(newChat);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    expect(
      screen.queryByRole("button", { name: "Sign out" }),
    ).not.toBeInTheDocument();
  });

  it("returns from settings to the blank start page when no session was active", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/v1/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "openai/gpt-4o",
                provider: "openai",
                resolved_provider: "openai",
                has_api_key: true,
              },
              providers: [{ name: "openai", label: "OpenAI", configured: true }],
              web_search: {
                provider: "duckduckgo",
                api_key_hint: null,
                base_url: null,
                providers: [
                  { name: "duckduckgo", label: "DuckDuckGo", credential: "none" },
                  { name: "brave", label: "Brave Search", credential: "api_key" },
                ],
              },
              runtime: {
                config_path: "/tmp/config.json",
              },
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(document.title).toBe("durin"));

    fireEvent.click(within(sidebar).getByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "General" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));

    await waitFor(() => expect(document.title).toBe("durin"));
    expect(screen.getByText("What can I do for you?")).toBeInTheDocument();
  });

  it("filters sidebar sessions through the lightweight search row", async () => {
    mockSessions = [
      {
        key: "websocket:chat-alpha",
        channel: "websocket",
        chatId: "chat-alpha",
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        title: "Q2 roadmap",
        preview: "Project planning notes",
      },
      {
        key: "websocket:chat-beta",
        channel: "websocket",
        chatId: "chat-beta",
        createdAt: "2026-04-15T10:00:00Z",
        updatedAt: "2026-04-15T10:00:00Z",
        preview: "Travel ideas",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    expect(within(sidebar).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(sidebar).getByText("Travel ideas")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "Search chats" }), {
      target: { value: "planning" },
    });

    expect(within(sidebar).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(sidebar).queryByText("Travel ideas")).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "Search chats" }), {
      target: { value: "road q2" },
    });

    expect(within(sidebar).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(sidebar).queryByText("Travel ideas")).not.toBeInTheDocument();
  });

  it("opens a blank start page without creating an empty chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    const matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("1024px"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    vi.stubGlobal("matchMedia", matchMedia);

    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Toggle theme from header" }));
    expect(toggleThemeSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    const desktopAside = container.querySelector("aside.lg\\:block") as HTMLElement;
    await waitFor(() => expect(desktopAside.style.width).toBe("0px"));

    expect(screen.queryByRole("button", { name: "Start a new chat" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Toggle sidebar" }));
    await waitFor(() => expect(desktopAside.style.width).toBe("272px"));

    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "New chat" }));
    expect(createChatSpy).not.toHaveBeenCalled();
    expect(screen.getByText("What can I do for you?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start a new chat" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Toggle theme from header" })).toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Settings" })).toBeInTheDocument();

    expect(within(sidebar).getByText("Existing chat")).toBeInTheDocument();
  });

  it("docks the voice orb in the shell when voice can speak", async () => {
    // The orb renders only once the config fetch confirms TTS is usable.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/v1/config")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              config: { voice: { enabled: true }, tts: { provider: "openai" } },
              schema: {},
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );
    render(<App />);
    await screen.findByText(/durin/i);
    expect(await screen.findByRole("button", { name: /voice|start voice/i })).toBeInTheDocument();
  });
});
