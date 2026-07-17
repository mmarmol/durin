import { type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowDown } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ThreadMessages } from "@/components/thread/ThreadMessages";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { UIMessage } from "@/lib/types";

interface ThreadViewportProps {
  messages: UIMessage[];
  isStreaming: boolean;
  composer: ReactNode;
  emptyState?: ReactNode;
  scrollToBottomSignal?: number;
  conversationKey?: string | null;
  onRetryLast?: () => void;
  onEditLastUser?: () => void;
  /** Fetch and prepend the next older page of history. */
  onLoadOlder?: () => void;
  /** ``true`` while there is an older page left to fetch. */
  hasOlder?: boolean;
  /** ``true`` while an older-page fetch is in flight. */
  loadingOlder?: boolean;
}

const NEAR_BOTTOM_PX = 48;
/** Distance from the top that arms the lazy older-history fetch. */
const NEAR_TOP_PX = 300;

export function ThreadViewport({
  messages,
  isStreaming,
  composer,
  emptyState,
  scrollToBottomSignal = 0,
  conversationKey = null,
  onRetryLast,
  onEditLastUser,
  onLoadOlder,
  hasOlder = false,
  loadingOlder = false,
}: ThreadViewportProps) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const lastConversationKeyRef = useRef<string | null>(conversationKey);
  const pendingConversationScrollRef = useRef(true);
  const scrollFrameIdsRef = useRef<number[]>([]);
  /** User scrolled away from the bottom; do not auto-yank until they return or we reset (new chat / send). */
  const userReadingHistoryRef = useRef(false);
  /** Captured scrollHeight/scrollTop just before an older page is requested,
   *  so the layout effect below can restore the visual scroll position once
   *  the prepended rows land — without this, prepending content above the
   *  viewport would otherwise yank the view downward. */
  const prependAnchorRef = useRef<{ height: number; top: number } | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const hasMessages = messages.length > 0;

  const cancelScheduledBottomScroll = useCallback(() => {
    for (const id of scrollFrameIdsRef.current) {
      window.cancelAnimationFrame(id);
    }
    scrollFrameIdsRef.current = [];
  }, []);

  const scrollToBottomNow = useCallback((smooth = false) => {
    const el = scrollRef.current;
    const marker = bottomRef.current;
    const behavior: ScrollBehavior = smooth ? "smooth" : "auto";
    if (marker) {
      marker.scrollIntoView({ block: "end", behavior });
    } else if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior });
    }
    setAtBottom(true);
  }, []);

  const scrollToBottom = useCallback(
    (smooth = false, frames = 1, options?: { force?: boolean }) => {
      const force = options?.force ?? false;
      cancelScheduledBottomScroll();
      const run = () => {
        if (!force && userReadingHistoryRef.current) return;
        scrollToBottomNow(smooth);
      };
      run();
      for (let i = 1; i < frames; i += 1) {
        const id = window.requestAnimationFrame(() => {
          if (!force && userReadingHistoryRef.current) return;
          scrollToBottomNow(smooth);
        });
        scrollFrameIdsRef.current.push(id);
      }
    },
    [cancelScheduledBottomScroll, scrollToBottomNow],
  );

  useEffect(() => {
    if (!atBottom) return;
    // A pending prepend anchor means these new `messages` are an older page
    // landing above the viewport, not fresh content at the bottom — hydrating
    // it must never yank the view down.
    if (prependAnchorRef.current) return;
    // Instant jump: CSS scroll-smooth + behavior "auto" still animates in some
    // browsers; session switches and history hydration should never slide from top.
    scrollToBottom(false);
  }, [messages, atBottom, scrollToBottom]);

  // Capture the pre-prepend scroll geometry the moment an older-page fetch
  // starts, so the layout effect below can restore the visual position once
  // the fetched rows are prepended to `messages`.
  useEffect(() => {
    if (!loadingOlder) return;
    const el = scrollRef.current;
    if (!el) return;
    prependAnchorRef.current = { height: el.scrollHeight, top: el.scrollTop };
  }, [loadingOlder]);

  useLayoutEffect(() => {
    const anchor = prependAnchorRef.current;
    if (!anchor) return;
    const el = scrollRef.current;
    prependAnchorRef.current = null;
    if (!el) return;
    el.scrollTop = el.scrollHeight - anchor.height + anchor.top;
  }, [messages]);

  useEffect(() => {
    if (scrollToBottomSignal <= 0) return;
    userReadingHistoryRef.current = false;
    scrollToBottom(false, 8);
  }, [scrollToBottomSignal, scrollToBottom]);

  useLayoutEffect(() => {
    if (lastConversationKeyRef.current === conversationKey) return;
    lastConversationKeyRef.current = conversationKey;
    pendingConversationScrollRef.current = true;
    userReadingHistoryRef.current = false;
    setAtBottom(true);
  }, [conversationKey]);

  useLayoutEffect(() => {
    if (!pendingConversationScrollRef.current) return;
    if (!conversationKey) {
      pendingConversationScrollRef.current = false;
      scrollToBottom(false, 4);
      return;
    }
    scrollToBottom(false, 8);
    if (!hasMessages) return;
    pendingConversationScrollRef.current = false;
  }, [conversationKey, hasMessages, messages, scrollToBottom]);

  useEffect(() => cancelScheduledBottomScroll, [cancelScheduledBottomScroll]);

  useEffect(() => {
    const target = contentRef.current;
    if (!target || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (userReadingHistoryRef.current) return;
      scrollToBottom(false, 4);
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMessages, scrollToBottom]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const updateBottomState = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      const near = distance < NEAR_BOTTOM_PX;
      setAtBottom(near);
      userReadingHistoryRef.current = !near;
    };

    // Only a genuine `scroll` event may trigger the older-page load — the
    // synchronous call below (used to seed `atBottom` at mount / whenever
    // this effect re-registers) must never do so, since it can run after the
    // conversation-open layout effect has already cleared the pending flag
    // for an already-hydrated session, defeating that guard.
    const onScroll = () => {
      updateBottomState();
      // Skip while the session-open bottom scroll hasn't run yet: the
      // viewport briefly sits at scrollTop 0 before that scroll fires, which
      // would otherwise misread as "user scrolled to top".
      if (pendingConversationScrollRef.current) return;
      if (el.scrollTop < NEAR_TOP_PX && hasOlder && !loadingOlder) {
        onLoadOlder?.();
      }
    };

    updateBottomState();
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [hasOlder, loadingOlder, onLoadOlder]);

  return (
    <div className="relative flex min-h-0 flex-1 overflow-hidden">
      <div
        ref={scrollRef}
        className={cn(
          "absolute inset-0 overflow-y-auto scroll-auto scrollbar-thin",
          "[&::-webkit-scrollbar]:w-1.5",
          "[&::-webkit-scrollbar-thumb]:rounded-full",
          "[&::-webkit-scrollbar-thumb]:bg-muted-foreground/30",
          "[&::-webkit-scrollbar-track]:bg-transparent",
        )}
      >
        {hasMessages ? (
          <div ref={contentRef} className="mx-auto flex min-h-full w-full max-w-[64rem] flex-col">
            <div className="flex-1 px-4 pb-20 pt-8">
              <div className="mx-auto w-full max-w-[49.5rem]">
                {hasOlder ? (
                  <div className="flex justify-center py-3 text-xs text-muted-foreground">
                    {loadingOlder ? t("thread.loadingHistory") : null}
                  </div>
                ) : (
                  <div className="flex justify-center py-3 text-xs text-muted-foreground">
                    {t("thread.historyStart")}
                  </div>
                )}
                <ThreadMessages
                  messages={messages}
                  isStreaming={isStreaming}
                  onRetryLast={onRetryLast}
                  onEditLastUser={onEditLastUser}
                />
              </div>
            </div>

            <div className="sticky bottom-0 z-10 mt-auto bg-background">
              <div className="px-4 pb-3">
                {composer}
              </div>
            </div>
          </div>
        ) : (
          <div ref={contentRef} className="mx-auto flex min-h-full w-full max-w-[72rem] flex-col px-4">
            <div className="flex w-full flex-1 items-center justify-center pb-[7vh] pt-8">
              <div className="flex w-full max-w-[58rem] flex-col gap-6">
                {emptyState}
                <div className="w-full">{composer}</div>
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} aria-hidden className="h-px" />
      </div>

      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-background to-transparent"
      />

      {!atBottom && (
        <Button
          variant="outline"
          size="icon"
          onClick={() => scrollToBottom(true, 1, { force: true })}
          className={cn(
            /* Keep clear of sticky composer (textarea + toolbar + optional goal strip). */
            "absolute bottom-48 left-1/2 z-20 h-8 w-8 -translate-x-1/2 rounded-full shadow-md",
            "bg-background/90 backdrop-blur",
            "animate-in fade-in-0 zoom-in-95",
          )}
          aria-label={t("thread.scrollToBottom")}
        >
          <ArrowDown className="h-4 w-4" />
        </Button>
      )}
    </div>
  );
}
