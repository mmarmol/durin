import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ThreadShell } from "@/components/thread/ThreadShell";
import { ThreadViewport } from "@/components/thread/ThreadViewport";
import { ClientProvider } from "@/providers/ClientProvider";
import type { UIMessage } from "@/lib/types";

const messages: UIMessage[] = [
  {
    id: "u1",
    role: "user",
    content: "hello",
    createdAt: Date.now(),
  },
];

function getScroller(container: HTMLElement): HTMLElement {
  return container.firstElementChild?.firstElementChild as HTMLElement;
}

function setGeometry(
  el: HTMLElement,
  geometry: { scrollHeight: number; clientHeight: number; scrollTop: number },
) {
  Object.defineProperties(el, {
    scrollHeight: { configurable: true, value: geometry.scrollHeight },
    clientHeight: { configurable: true, value: geometry.clientHeight },
    scrollTop: { configurable: true, writable: true, value: geometry.scrollTop },
  });
}

describe("ThreadViewport pagination", () => {
  it("fires onLoadOlder once per near-top scroll, not repeatedly while loadingOlder", () => {
    const onLoadOlder = vi.fn();
    const { container, rerender } = render(
      <ThreadViewport
        messages={messages}
        isStreaming={false}
        composer={<div />}
        conversationKey="chat-a"
        hasOlder
        loadingOlder={false}
        onLoadOlder={onLoadOlder}
      />,
    );
    const scroller = getScroller(container);
    setGeometry(scroller, { scrollHeight: 2000, clientHeight: 600, scrollTop: 0 });

    act(() => {
      scroller.dispatchEvent(new Event("scroll"));
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // Parent starts the fetch: loadingOlder flips true. Further near-top
    // scroll events during the same fetch must not re-fire.
    rerender(
      <ThreadViewport
        messages={messages}
        isStreaming={false}
        composer={<div />}
        conversationKey="chat-a"
        hasOlder
        loadingOlder
        onLoadOlder={onLoadOlder}
      />,
    );
    act(() => {
      scroller.dispatchEvent(new Event("scroll"));
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    // Fetch resolves; a further near-top scroll may fire again.
    rerender(
      <ThreadViewport
        messages={messages}
        isStreaming={false}
        composer={<div />}
        conversationKey="chat-a"
        hasOlder
        loadingOlder={false}
        onLoadOlder={onLoadOlder}
      />,
    );
    act(() => {
      scroller.dispatchEvent(new Event("scroll"));
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(2);
  });

  it("suppresses onLoadOlder until the session-open bottom scroll has resolved", () => {
    // A session opens with no messages hydrated yet: the viewport briefly
    // sits at scrollTop 0 before the initial bottom-scroll runs. That must
    // not be misread as "user scrolled to top".
    const onLoadOlder = vi.fn();
    const { container, rerender } = render(
      <ThreadViewport
        messages={[]}
        isStreaming={false}
        composer={<div />}
        conversationKey="chat-pending"
        hasOlder
        loadingOlder={false}
        onLoadOlder={onLoadOlder}
      />,
    );
    const scroller = getScroller(container);
    setGeometry(scroller, { scrollHeight: 0, clientHeight: 0, scrollTop: 0 });

    act(() => {
      scroller.dispatchEvent(new Event("scroll"));
    });
    expect(onLoadOlder).not.toHaveBeenCalled();

    // Messages hydrate: the pending flag clears (see ThreadViewport's
    // conversation-open layout effect), so a subsequent near-top scroll is
    // now honored.
    rerender(
      <ThreadViewport
        messages={messages}
        isStreaming={false}
        composer={<div />}
        conversationKey="chat-pending"
        hasOlder
        loadingOlder={false}
        onLoadOlder={onLoadOlder}
      />,
    );
    setGeometry(scroller, { scrollHeight: 2000, clientHeight: 600, scrollTop: 0 });
    act(() => {
      scroller.dispatchEvent(new Event("scroll"));
    });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it("renders the beginning-of-conversation label when there is no older page", () => {
    render(
      <ThreadViewport
        messages={messages}
        isStreaming={false}
        composer={<div />}
        conversationKey="chat-a"
        hasOlder={false}
      />,
    );
    expect(screen.getByText("Beginning of conversation")).toBeInTheDocument();
  });

  it("shows the loading-history label while an older page is in flight", () => {
    render(
      <ThreadViewport
        messages={messages}
        isStreaming={false}
        composer={<div />}
        conversationKey="chat-a"
        hasOlder
        loadingOlder
      />,
    );
    expect(screen.getByText("Loading earlier messages…")).toBeInTheDocument();
    expect(screen.queryByText("Beginning of conversation")).not.toBeInTheDocument();
  });

  it("preserves visual scroll position when an older page is prepended", () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollIntoView;

    try {
      const { container, rerender } = render(
        <ThreadViewport
          messages={messages}
          isStreaming={false}
          composer={<div />}
          conversationKey="chat-anchor"
          hasOlder
          loadingOlder={false}
        />,
      );
      const scroller = getScroller(container);
      setGeometry(scroller, { scrollHeight: 1000, clientHeight: 500, scrollTop: 200 });
      scrollIntoView.mockClear();

      // Parent starts the older-page fetch: loadingOlder flips true, which
      // captures {height: 1000, top: 200} as the pre-prepend anchor.
      rerender(
        <ThreadViewport
          messages={messages}
          isStreaming={false}
          composer={<div />}
          conversationKey="chat-anchor"
          hasOlder
          loadingOlder
        />,
      );

      // The fetch resolves: older rows are prepended (content grows by 600px)
      // and loadingOlder clears.
      const olderMessage: UIMessage = {
        id: "hist-100-0",
        role: "assistant",
        content: "an older reply",
        createdAt: 0,
      };
      Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: 1600 });
      rerender(
        <ThreadViewport
          messages={[olderMessage, ...messages]}
          isStreaming={false}
          composer={<div />}
          conversationKey="chat-anchor"
          hasOlder
          loadingOlder={false}
        />,
      );

      // newScrollTop = newScrollHeight - anchorHeight + anchorTop = 1600 - 1000 + 200
      expect(scroller.scrollTop).toBe(800);
      // The auto-scroll-to-bottom effect must not have yanked the view down
      // while hydrating the older page.
      expect(scrollIntoView).not.toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    }
  });
});

// --- ThreadShell integration: the loaded page must PERSIST, not just appear.
// ThreadShell owns a historical→messages resync effect that re-fires on the
// same `historical` growth the older-page load produces; if the older rows
// only ever reach the live `messages` state (and not the message cache that
// effect restores from), the resync overwrites them right back out.

function makeClient() {
  const errorHandlers = new Set<(err: { kind: string }) => void>();
  const sessionUpdateHandlers = new Set<(chatId: string) => void>();
  return {
    status: "open" as const,
    defaultChatId: null as string | null,
    onStatus: () => () => {},
    onRuntimeModelUpdate: () => () => {},
    getRunStartedAt: () => null,
    getGoalState: () => undefined,
    onChat: () => () => {},
    onError: (handler: (err: { kind: string }) => void) => {
      errorHandlers.add(handler);
      return () => {
        errorHandlers.delete(handler);
      };
    },
    onSessionUpdate: (handler: (chatId: string) => void) => {
      sessionUpdateHandlers.add(handler);
      return () => {
        sessionUpdateHandlers.delete(handler);
      };
    },
    sendMessage: vi.fn(),
    newChat: vi.fn(),
    attach: vi.fn(),
    connect: vi.fn(),
    close: vi.fn(),
    updateUrl: vi.fn(),
  };
}

function wrap(client: ReturnType<typeof makeClient>, children: ReactNode) {
  return (
    <ClientProvider
      client={client as unknown as import("@/lib/durin-client").DurinClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

function session(chatId: string) {
  return {
    key: `websocket:${chatId}`,
    channel: "websocket" as const,
    chatId,
    createdAt: null,
    updatedAt: null,
    preview: "",
  };
}

function httpJson(body: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  };
}

describe("ThreadShell pagination integration", () => {
  it("keeps the loaded older page after all effects settle", async () => {
    const client = makeClient();
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("websocket%3Achat-pg/webui-thread")) {
          if (url.includes("before=")) {
            return httpJson({
              schemaVersion: 4,
              prevCursor: null,
              messages: [
                { id: "old-1", role: "user", content: "an older question", createdAt: 500 },
                { id: "old-2", role: "assistant", content: "an older answer", createdAt: 501 },
              ],
            });
          }
          return httpJson({
            schemaVersion: 4,
            prevCursor: 4096,
            messages: [
              { id: "new-1", role: "user", content: "latest question", createdAt: 1000 },
              { id: "new-2", role: "assistant", content: "latest answer", createdAt: 1001 },
            ],
          });
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    try {
      const { container } = render(
        wrap(
          client,
          <ThreadShell
            session={session("chat-pg")}
            title="Chat chat-pg"
            onToggleSidebar={() => {}}
            onNewChat={() => {}}
          />,
        ),
      );

      await waitFor(() => expect(screen.getByText("latest answer")).toBeInTheDocument());

      const scroller = Array.from(
        container.querySelectorAll<HTMLElement>("div.overflow-y-auto"),
      ).find((el) => el.textContent?.includes("latest answer"));
      expect(scroller).toBeDefined();
      Object.defineProperties(scroller as HTMLElement, {
        scrollHeight: { configurable: true, value: 2000 },
        clientHeight: { configurable: true, value: 600 },
        scrollTop: { configurable: true, writable: true, value: 0 },
      });
      scrollIntoView.mockClear();

      // User scrolls near the top: the older page is fetched and prepended.
      act(() => {
        (scroller as HTMLElement).dispatchEvent(new Event("scroll"));
      });
      await waitFor(() => expect(screen.getByText("an older question")).toBeInTheDocument());

      // The critical assertion: let every reactive effect settle (including
      // ThreadShell's historical→messages resync, which fires on the same
      // `historical` change) and require the older page to STILL be present.
      await act(async () => {});
      await act(async () => {});
      expect(screen.getByText("an older question")).toBeInTheDocument();
      expect(screen.getByText("an older answer")).toBeInTheDocument();
      expect(screen.getByText("latest question")).toBeInTheDocument();
      expect(screen.getByText("latest answer")).toBeInTheDocument();

      // The older fetch used the cursor from the newest page.
      const fetchMock = fetch as ReturnType<typeof vi.fn>;
      const beforeCalls = fetchMock.mock.calls
        .map((c) => String(c[0]))
        .filter((u) => u.includes("before="));
      expect(beforeCalls).toHaveLength(1);
      expect(beforeCalls[0]).toContain("before=4096");

      // Hydrating the older page must not have scrolled the view to the
      // bottom (the user is reading history near the top).
      expect(scrollIntoView).not.toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  it("does not duplicate the older page after a session round-trip", async () => {
    // Load older in A → switch to B → back to A: the live thread restores
    // the MERGED list from the message cache, but useSessionHistory refetches
    // fresh and re-arms prevCursor at the same offset. A second near-top
    // scroll then fetches the SAME before= page; the splice must be
    // idempotent (skip rows whose ids are already present) or the older page
    // renders twice with duplicate React keys.
    const client = makeClient();
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = vi.fn();
    const consoleErrorSpy = vi.spyOn(console, "error");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("websocket%3Achat-rt/webui-thread")) {
          if (url.includes("before=")) {
            return httpJson({
              schemaVersion: 4,
              prevCursor: null,
              messages: [
                { id: "old-1", role: "user", content: "an older question", createdAt: 500 },
                { id: "old-2", role: "assistant", content: "an older answer", createdAt: 501 },
              ],
            });
          }
          return httpJson({
            schemaVersion: 4,
            prevCursor: 4096,
            messages: [
              { id: "new-1", role: "user", content: "latest question", createdAt: 1000 },
              { id: "new-2", role: "assistant", content: "latest answer", createdAt: 1001 },
            ],
          });
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    function shell(chatId: string) {
      return (
        <ThreadShell
          session={session(chatId)}
          title={`Chat ${chatId}`}
          onToggleSidebar={() => {}}
          onNewChat={() => {}}
        />
      );
    }

    function findScroller(container: HTMLElement): HTMLElement {
      const el = Array.from(
        container.querySelectorAll<HTMLElement>("div.overflow-y-auto"),
      ).find((node) => node.textContent?.includes("latest answer"));
      expect(el).toBeDefined();
      return el as HTMLElement;
    }

    function scrollNearTop(scroller: HTMLElement) {
      Object.defineProperties(scroller, {
        scrollHeight: { configurable: true, value: 2000 },
        clientHeight: { configurable: true, value: 600 },
        scrollTop: { configurable: true, writable: true, value: 0 },
      });
      act(() => {
        scroller.dispatchEvent(new Event("scroll"));
      });
    }

    try {
      const { container, rerender } = render(wrap(client, shell("chat-rt")));
      await waitFor(() => expect(screen.getByText("latest answer")).toBeInTheDocument());

      // First older-page load in A.
      scrollNearTop(findScroller(container));
      await waitFor(() => expect(screen.getByText("an older question")).toBeInTheDocument());

      // Round-trip: A → B → A.
      await act(async () => {
        rerender(wrap(client, shell("chat-rt-b")));
      });
      await act(async () => {
        rerender(wrap(client, shell("chat-rt")));
      });
      // The merged thread is restored from the cache (older page included)
      // while the refetched history re-arms prevCursor at the same offset.
      await waitFor(() => expect(screen.getByText("latest answer")).toBeInTheDocument());
      await waitFor(() => expect(screen.getByText("an older question")).toBeInTheDocument());
      consoleErrorSpy.mockClear();

      // Second near-top scroll: fetches the SAME before= page again.
      scrollNearTop(findScroller(container));
      await waitFor(() => {
        const beforeCalls = (fetch as ReturnType<typeof vi.fn>).mock.calls
          .map((c) => String(c[0]))
          .filter((u) => u.includes("before="));
        expect(beforeCalls).toHaveLength(2);
      });
      await act(async () => {});
      await act(async () => {});

      // Every row still renders exactly once…
      expect(screen.getAllByText("an older question")).toHaveLength(1);
      expect(screen.getAllByText("an older answer")).toHaveLength(1);
      expect(screen.getAllByText("latest question")).toHaveLength(1);
      expect(screen.getAllByText("latest answer")).toHaveLength(1);
      // …and React never warned about duplicate keys.
      const duplicateKeyWarnings = consoleErrorSpy.mock.calls.filter((call) =>
        call.map(String).join(" ").includes("same key"),
      );
      expect(duplicateKeyWarnings).toHaveLength(0);
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
      consoleErrorSpy.mockRestore();
    }
  });
});
