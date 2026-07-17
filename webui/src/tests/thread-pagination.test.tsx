import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ThreadShell } from "@/components/thread/ThreadShell";
import { ThreadViewport } from "@/components/thread/ThreadViewport";
import { ClientProvider } from "@/providers/ClientProvider";
import { PrependPin } from "@/lib/prepend-pin";
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

  it("renders every message row with a data-message-id (clusters list all members)", () => {
    const thread: UIMessage[] = [
      { id: "t1", role: "assistant", content: "using tool", kind: "trace", createdAt: 1 },
      { id: "t2", role: "assistant", content: "using another", kind: "trace", createdAt: 2 },
      { id: "u9", role: "user", content: "a question", createdAt: 3 },
      { id: "a9", role: "assistant", content: "an answer", createdAt: 4 },
    ];
    const { container } = render(
      <ThreadViewport
        messages={thread}
        isStreaming={false}
        composer={<div />}
        conversationKey="chat-ids"
      />,
    );

    // Singles carry their own id.
    expect(container.querySelector('[data-message-id="u9"]')).not.toBeNull();
    expect(container.querySelector('[data-message-id="a9"]')).not.toBeNull();
    // The trace cluster row is identified by its first member and lists every
    // member, so a pin anchored on any of them can re-acquire the row.
    const cluster = container.querySelector('[data-message-id="t1"]');
    expect(cluster).not.toBeNull();
    expect(cluster?.getAttribute("data-message-ids")).toBe("t1 t2");
    expect(container.querySelector('[data-message-ids~="t2"]')).toBe(cluster);
  });

  it("pins the pre-prepend first message across the prepend AND later reflow", async () => {
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
      // The anchor is the first rendered message row (ThreadMessages root's
      // first child). jsdom has no layout, so model one: the anchor sits at
      // a document offset and its viewport top is offset - scrollTop, which
      // makes pin restores converge exactly as in a real browser.
      const messagesRoot = container.querySelector(
        'div[class="flex w-full flex-col"]',
      ) as HTMLElement;
      const anchorRow = messagesRoot.firstElementChild as HTMLElement;
      let anchorOffset = 300; // viewport top 100 at scrollTop 200
      anchorRow.getBoundingClientRect = () =>
        ({ top: anchorOffset - scroller.scrollTop }) as DOMRect;
      scrollIntoView.mockClear();

      // Parent starts the older-page fetch: loadingOlder flips true, which
      // records the anchor row at viewport top 100 with scrollTop 200.
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

      // The fetch resolves: older rows are prepended, pushing the anchor row
      // down by 600px (viewport top 700 at the still-unchanged scrollTop).
      const olderMessage: UIMessage = {
        id: "hist-100-0",
        role: "assistant",
        content: "an older reply",
        createdAt: 0,
      };
      anchorOffset = 900;
      const prepended = [olderMessage, ...messages];
      rerender(
        <ThreadViewport
          messages={prepended}
          isStreaming={false}
          composer={<div />}
          conversationKey="chat-anchor"
          hasOlder
          loadingOlder={false}
        />,
      );

      // First restore: scrollTop += (700 - 100) = 200 + 600.
      expect(scroller.scrollTop).toBe(800);

      // Step past the same-frame duplicate-apply guard before the next tick.
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 10));
      });

      // Post-prepend reflow (progressive markdown/image layout) pushes the
      // anchor again AFTER the first restore — the one-shot compensation
      // would stop here; the pin must keep restoring.
      anchorOffset += 150; // viewport top 250 at scrollTop 800
      rerender(
        <ThreadViewport
          messages={[...prepended]}
          isStreaming={false}
          composer={<div />}
          conversationKey="chat-anchor"
          hasOlder
          loadingOlder={false}
        />,
      );
      // Second restore: scrollTop += (250 - 100) = 800 + 150.
      expect(scroller.scrollTop).toBe(950);

      // Async late layout with NO further render or resize tick: only the
      // low-frequency safety interval is left watching — it must restore.
      anchorOffset += 80; // viewport top 180 at scrollTop 950
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 300));
      });
      // Interval restore: scrollTop += (180 - 100) = 950 + 80.
      expect(scroller.scrollTop).toBe(1030);

      // The auto-scroll-to-bottom effect must not have yanked the view down
      // at any point in the pinning window.
      expect(scrollIntoView).not.toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    }
  });
});

describe("PrependPin", () => {
  function makeAnchor(top: number) {
    const state = { top, connected: true };
    const anchor = {
      get isConnected() {
        return state.connected;
      },
      getBoundingClientRect: () => ({ top: state.top }),
    };
    return { state, anchor };
  }

  it("re-applies the restore across successive layout ticks", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    state.top = 700; // prepend pushed the anchor down
    expect(pin.apply(scroller, 10)).toBe(true);
    expect(scroller.scrollTop).toBe(800);

    state.top = 235; // late reflow moved it again
    expect(pin.apply(scroller, 20)).toBe(true);
    expect(scroller.scrollTop).toBe(935);
  });

  it("holds through stable ticks and still catches late reflow after a quiet gap", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    // Layout goes quiet: many consecutive no-adjustment ticks. Stability
    // must NOT release the pin — async late layout (image fallbacks after
    // 404s, markdown settling) can land after the quiet gap.
    expect(pin.apply(scroller, 10)).toBe(true);
    expect(pin.apply(scroller, 130)).toBe(true);
    expect(pin.apply(scroller, 250)).toBe(true);
    expect(scroller.scrollTop).toBe(200);

    // The late reflow lands: the pin is still watching and restores.
    state.top = 400;
    expect(pin.apply(scroller, 370)).toBe(true);
    expect(scroller.scrollTop).toBe(500);
    state.top = 100;
    expect(pin.apply(scroller, 490)).toBe(true); // stable again, still held
    expect(scroller.scrollTop).toBe(500);
  });

  it("skips a duplicate apply within the same frame as a no-op", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    state.top = 700;
    expect(pin.apply(scroller, 10)).toBe(true);
    expect(scroller.scrollTop).toBe(800);

    // A second caller observes the same commit ~1 ms later (double layout
    // effect from back-to-back content-identical commits, or layout effect
    // + ResizeObserver): geometry cannot have re-laid-out — no-op, held.
    state.top = 900;
    expect(pin.apply(scroller, 11)).toBe(true);
    expect(scroller.scrollTop).toBe(800);

    // A genuinely later tick applies against fresh geometry.
    expect(pin.apply(scroller, 30)).toBe(true);
    expect(scroller.scrollTop).toBe(1600); // 800 + (900 - 100)
  });

  it("releases without adjusting when the user scrolled themselves", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    expect(pin.notifyScroll(200)).toBe(true); // our own position: still active
    expect(pin.notifyScroll(999)).toBe(false); // not ours: user wins

    state.top = 700;
    scroller.scrollTop = 999; // user scrolled before this tick
    expect(pin.apply(scroller, 10)).toBe(false);
    expect(scroller.scrollTop).toBe(999); // untouched
  });

  it("tracks its own restores so they do not read as user scrolls", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    state.top = 700;
    expect(pin.apply(scroller, 10)).toBe(true);
    // The scroll event fired by the pin's own restore reports the value the
    // pin set — that must not release it.
    expect(pin.notifyScroll(scroller.scrollTop)).toBe(true);
  });

  it("releases on genuine quiet: enough executed no-op ticks over a real time span", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    state.top = 700;
    expect(pin.apply(scroller, 10)).toBe(true); // adjusting restore → 800
    expect(scroller.scrollTop).toBe(800);

    // Quiet ticks: held while EITHER condition is unmet.
    state.top = 100;
    expect(pin.apply(scroller, 130)).toBe(true); // no-op tick 1
    expect(pin.apply(scroller, 250)).toBe(true); // no-op tick 2
    expect(pin.apply(scroller, 370)).toBe(true); // no-op tick 3
    expect(pin.apply(scroller, 500)).toBe(true); // tick 4 but only 490ms since adjust
    expect(pin.apply(scroller, 700)).toBe(false); // tick 5, 690ms quiet: released
    expect(scroller.scrollTop).toBe(800);
  });

  it("an adjusting restore resets the quiet window", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    state.top = 700;
    expect(pin.apply(scroller, 10)).toBe(true); // adjust → 800
    state.top = 100;
    expect(pin.apply(scroller, 130)).toBe(true); // no-op 1
    expect(pin.apply(scroller, 250)).toBe(true); // no-op 2
    expect(pin.apply(scroller, 370)).toBe(true); // no-op 3
    state.top = 180; // late reflow: adjusting restore resets ticks AND clock
    expect(pin.apply(scroller, 640)).toBe(true);
    expect(scroller.scrollTop).toBe(880);
    state.top = 100;
    expect(pin.apply(scroller, 760)).toBe(true); // no-op 1 of the NEW window
    expect(pin.apply(scroller, 880)).toBe(true);
    expect(pin.apply(scroller, 1000)).toBe(true);
    expect(pin.apply(scroller, 1120)).toBe(true); // tick 4 but only 480ms since adjust
    expect(pin.apply(scroller, 1360)).toBe(false); // 720ms quiet: released
  });

  it("survives main-thread starvation: a long tickless gap must not expire the pin", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    // First restore, then the prepend's own layout burst blocks the main
    // thread for ~2s: no observer tick, no interval tick can run.
    state.top = 700;
    expect(pin.apply(scroller, 100)).toBe(true);
    expect(scroller.scrollTop).toBe(800);

    // Execution resumes; the async late reflow has landed meanwhile. The
    // next tick must STILL apply and adjust — wall-clock alone (2s > any
    // quiet span) must not have released the pin during the gap.
    state.top = 400;
    expect(pin.apply(scroller, 2100)).toBe(true);
    expect(scroller.scrollTop).toBe(1100);

    // Genuine quiet after the late reflow: releases normally.
    state.top = 100;
    expect(pin.apply(scroller, 2220)).toBe(true);
    expect(pin.apply(scroller, 2340)).toBe(true);
    expect(pin.apply(scroller, 2460)).toBe(true);
    expect(pin.apply(scroller, 2820)).toBe(false); // 4 ticks, 720ms quiet
    expect(scroller.scrollTop).toBe(1100);
  });

  it("releases at the pathological ceiling, counted from the first restore tick", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200, null, 1500);

    // A slow fetch delays the first tick — the pin must still be usable.
    expect(pin.started).toBe(false);
    state.top = 700;
    expect(pin.apply(scroller, 10_000)).toBe(true);
    expect(pin.started).toBe(true);
    expect(scroller.scrollTop).toBe(800);

    // Within the window: keeps restoring (adjusting, so quiet never trips).
    state.top = 250;
    expect(pin.apply(scroller, 11_400)).toBe(true);
    expect(scroller.scrollTop).toBe(950);

    // Past the ceiling (10_000 + 1500): released without adjusting.
    state.top = 700;
    expect(pin.apply(scroller, 11_501)).toBe(false);
    expect(scroller.scrollTop).toBe(950);
  });

  it("releases when the anchor unmounts and no reacquire callback exists", () => {
    const { state, anchor } = makeAnchor(100);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(anchor, 100, 200);

    state.connected = false;
    state.top = 700;
    expect(pin.apply(scroller, 10)).toBe(false);
    expect(scroller.scrollTop).toBe(200); // untouched
  });

  it("re-acquires its anchor by identity when the element remounts, keeping recordedTop", () => {
    const first = makeAnchor(100);
    const replacement = makeAnchor(0);
    const scroller = { scrollTop: 200 };
    const pin = new PrependPin(first.anchor, 100, 200, () => replacement.anchor);

    // Normal restore against the original element.
    first.state.top = 700;
    expect(pin.apply(scroller, 10)).toBe(true);
    expect(scroller.scrollTop).toBe(800);

    // The prepend re-clustered the row: original node unmounts, the merged
    // row (containing the same message) renders lower in the viewport.
    first.state.connected = false;
    replacement.state.top = 350;
    expect(pin.apply(scroller, 20)).toBe(true);
    // Restored against the SAME recordedTop (100): 800 + (350 - 100).
    expect(scroller.scrollTop).toBe(1050);

    // Further ticks keep using the re-acquired element.
    replacement.state.top = 130;
    expect(pin.apply(scroller, 30)).toBe(true);
    expect(scroller.scrollTop).toBe(1080);
  });

  it("releases when re-acquire fails or yields a disconnected element", () => {
    const gone = makeAnchor(100);
    const scrollerA = { scrollTop: 200 };
    const pinNull = new PrependPin(gone.anchor, 100, 200, () => null);
    gone.state.connected = false;
    gone.state.top = 700;
    expect(pinNull.apply(scrollerA, 10)).toBe(false);
    expect(scrollerA.scrollTop).toBe(200); // untouched

    const alsoGone = makeAnchor(100);
    const detached = makeAnchor(500);
    detached.state.connected = false;
    const scrollerB = { scrollTop: 200 };
    const pinDetached = new PrependPin(alsoGone.anchor, 100, 200, () => detached.anchor);
    alsoGone.state.connected = false;
    expect(pinDetached.apply(scrollerB, 10)).toBe(false);
    expect(scrollerB.scrollTop).toBe(200); // untouched
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
          // Ids shaped like the real (fixed) backend: fallback replay ids
          // namespaced by the page's own byte offset (see
          // replay_transcript_to_ui_messages / build_webui_thread_response in
          // durin/utils/webui_transcript.py) so two pages never collide.
          if (url.includes("before=")) {
            return httpJson({
              schemaVersion: 4,
              prevCursor: null,
              messages: [
                { id: "p0-u-0", role: "user", content: "an older question", createdAt: 500 },
                { id: "p0-as-1", role: "assistant", content: "an older answer", createdAt: 501 },
              ],
            });
          }
          return httpJson({
            schemaVersion: 4,
            prevCursor: 4096,
            messages: [
              { id: "p4096-u-0", role: "user", content: "latest question", createdAt: 1000 },
              { id: "p4096-as-1", role: "assistant", content: "latest answer", createdAt: 1001 },
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
          // Same backend-shaped, page-namespaced ids as above.
          if (url.includes("before=")) {
            return httpJson({
              schemaVersion: 4,
              prevCursor: null,
              messages: [
                { id: "p0-u-0", role: "user", content: "an older question", createdAt: 500 },
                { id: "p0-as-1", role: "assistant", content: "an older answer", createdAt: 501 },
              ],
            });
          }
          return httpJson({
            schemaVersion: 4,
            prevCursor: 4096,
            messages: [
              { id: "p4096-u-0", role: "user", content: "latest question", createdAt: 1000 },
              { id: "p4096-as-1", role: "assistant", content: "latest answer", createdAt: 1001 },
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

  it("never renders duplicates when the backend sends colliding replay ids across pages", async () => {
    // Regression pin for a misbehaving backend: a correct backend namespaces
    // fallback replay ids by page offset (see the two tests above) so this
    // can never legitimately happen, but if it ever did, the frontend's
    // idempotent splice (dedupe by id in ThreadShell's handleLoadOlder) is
    // the last line of defense. Colliding ids mean the older page reads as
    // "already present" and gets silently skipped — content hidden is
    // acceptable here, duplicate rendering is not.
    const client = makeClient();
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = vi.fn();
    const consoleErrorSpy = vi.spyOn(console, "error");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("websocket%3Achat-collision/webui-thread")) {
          if (url.includes("before=")) {
            return httpJson({
              schemaVersion: 4,
              prevCursor: null,
              messages: [
                { id: "u-0", role: "user", content: "an older question", createdAt: 500 },
                { id: "as-1", role: "assistant", content: "an older answer", createdAt: 501 },
              ],
            });
          }
          return httpJson({
            schemaVersion: 4,
            prevCursor: 4096,
            messages: [
              { id: "u-0", role: "user", content: "latest question", createdAt: 1000 },
              { id: "as-1", role: "assistant", content: "latest answer", createdAt: 1001 },
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
            session={session("chat-collision")}
            title="Chat chat-collision"
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

      act(() => {
        (scroller as HTMLElement).dispatchEvent(new Event("scroll"));
      });
      await waitFor(() => {
        const beforeCalls = (fetch as ReturnType<typeof vi.fn>).mock.calls
          .map((c) => String(c[0]))
          .filter((u) => u.includes("before="));
        expect(beforeCalls).toHaveLength(1);
      });
      await act(async () => {});
      await act(async () => {});

      // Colliding ids make the older rows indistinguishable from the already-
      // present newest rows, so the idempotent splice skips them — the older
      // page's text never appears. Documenting that cost, not asserting it as
      // desirable: the invariant that matters is no duplicate rendering.
      expect(screen.queryByText("an older question")).not.toBeInTheDocument();
      expect(screen.getAllByText("latest question")).toHaveLength(1);
      expect(screen.getAllByText("latest answer")).toHaveLength(1);
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
