import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ThreadViewport } from "@/components/thread/ThreadViewport";
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
