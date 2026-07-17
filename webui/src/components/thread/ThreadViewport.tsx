import { type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowDown } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ThreadMessages } from "@/components/thread/ThreadMessages";
import { Button } from "@/components/ui/button";
import { PIN_SAFETY_TICK_MS, PrependPin } from "@/lib/prepend-pin";
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
  /** Wrapper around the rendered message list; its first row is the anchor
   *  element pinned across an older-page prepend. */
  const messagesRef = useRef<HTMLDivElement>(null);
  /** Active prepend pin: keeps the pre-prepend first message at its recorded
   *  viewport position across the prepend AND the progressive layout that
   *  follows it (markdown/image reflow keeps growing the DOM after the first
   *  restore frame, so a one-shot compensation lands the view off by the
   *  post-snapshot growth). Non-null for the whole pinning window. */
  const pinRef = useRef<PrependPin | null>(null);
  /** Safety-tick interval id while a pin is active (see applyPinTick). */
  const pinTimerRef = useRef<number | null>(null);
  /** Self-reference so the interval callback always runs the latest tick fn. */
  const applyPinTickRef = useRef<() => boolean>(() => false);
  const [atBottom, setAtBottom] = useState(true);
  const hasMessages = messages.length > 0;

  const releasePin = useCallback(() => {
    pinRef.current = null;
    if (pinTimerRef.current !== null) {
      window.clearInterval(pinTimerRef.current);
      pinTimerRef.current = null;
    }
  }, []);

  /** Run one pin-restore tick. Returns true when the tick belonged to the
   *  pin (callers must then skip their own scroll behavior), releasing the
   *  pin when it reports itself done. */
  const applyPinTick = useCallback((): boolean => {
    const pin = pinRef.current;
    if (!pin) return false;
    const el = scrollRef.current;
    if (!el) {
      releasePin();
      return false;
    }
    if (!pin.apply(el, performance.now())) {
      releasePin();
      return true;
    }
    // Low-frequency safety tick, armed at the first restore: async late
    // layout (image fallbacks after 404s, markdown settling) can land after
    // the ResizeObserver has gone quiet, so the observer alone cannot be
    // trusted to deliver a tick for it. The interval keeps applying until
    // the pin releases itself (deadline / user scroll / anchor loss), which
    // also bounds how long the interval lives; releasePin clears it.
    if (pinTimerRef.current === null) {
      pinTimerRef.current = window.setInterval(
        () => applyPinTickRef.current(),
        PIN_SAFETY_TICK_MS,
      );
    }
    return true;
  }, [releasePin]);
  applyPinTickRef.current = applyPinTick;

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
    // An active pin means these new `messages` are an older page landing
    // above the viewport, not fresh content at the bottom — hydrating it
    // must never yank the view down, for the WHOLE pinning window (the DOM
    // keeps reflowing well past the first post-prepend frame).
    if (pinRef.current) return;
    // Instant jump: CSS scroll-smooth + behavior "auto" still animates in some
    // browsers; session switches and history hydration should never slide from top.
    scrollToBottom(false);
  }, [messages, atBottom, scrollToBottom]);

  // Arm the pin the moment an older-page fetch starts: record the first
  // message row, its message id, and its viewport position, so every later
  // layout tick can restore it there. The id is the durable identity: when
  // the prepend swaps the row's DOM node (prepended rows re-clustering with
  // the previously-first row is the common case), the pin re-acquires the
  // element for the same message — by exact row id first, then by cluster
  // membership (a merged cluster lists all members in data-message-ids).
  useEffect(() => {
    if (!loadingOlder) return;
    const el = scrollRef.current;
    const anchor = messagesRef.current?.querySelector("[data-message-id]");
    if (!el || !anchor) return;
    const anchorId = anchor.getAttribute("data-message-id");
    const escaped = anchorId !== null && typeof CSS !== "undefined" && typeof CSS.escape === "function"
      ? CSS.escape(anchorId)
      : anchorId;
    const reacquire = escaped
      ? () => {
          const root = messagesRef.current;
          if (!root) return null;
          return (
            root.querySelector(`[data-message-id="${escaped}"]`)
            ?? root.querySelector(`[data-message-ids~="${escaped}"]`)
          );
        }
      : null;
    releasePin();
    pinRef.current = new PrependPin(
      anchor,
      anchor.getBoundingClientRect().top,
      el.scrollTop,
      reacquire,
    );
  }, [loadingOlder, releasePin]);

  // First restore, synchronously with the prepend commit.
  useLayoutEffect(() => {
    if (!pinRef.current) return;
    applyPinTick();
  }, [messages, applyPinTick]);

  // A fetch that ends without a prepend (failure / empty page) never runs a
  // restore tick: drop its pin, or the auto-scroll guard would stay latched.
  // On success this is a no-op — the prepend commits in the same batch that
  // clears `loadingOlder`, and its layout effect (above) runs first.
  useEffect(() => {
    if (loadingOlder) return;
    const pin = pinRef.current;
    if (pin && !pin.started) releasePin();
  }, [loadingOlder, releasePin]);

  useEffect(() => releasePin, [releasePin]);

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
    releasePin();
    setAtBottom(true);
  }, [conversationKey, releasePin]);

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
      // While a prepend pin is active, every size change is post-prepend
      // reflow (progressive markdown/image layout): the tick re-restores the
      // anchor instead of driving the follow-the-bottom behavior.
      if (applyPinTick()) return;
      if (userReadingHistoryRef.current) return;
      scrollToBottom(false, 4);
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMessages, scrollToBottom, applyPinTick]);

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
      // A scroll position the active pin did not set itself is the user's
      // own scrolling — their intent wins over any further pin restores.
      const pin = pinRef.current;
      if (pin && !pin.notifyScroll(el.scrollTop)) {
        releasePin();
      }
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
  }, [hasOlder, loadingOlder, onLoadOlder, releasePin]);

  return (
    <div className="relative flex min-h-0 flex-1 overflow-hidden">
      <div
        ref={scrollRef}
        className={cn(
          "absolute inset-0 overflow-y-auto scroll-auto scrollbar-thin",
          /* The prepend pin is the single scroll-anchoring authority; the
           * browser's native anchoring would double-compensate and its
           * adjustments would register as user scrolls, breaking the pin. */
          "[overflow-anchor:none]",
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
                <div ref={messagesRef}>
                  <ThreadMessages
                    messages={messages}
                    isStreaming={isStreaming}
                    onRetryLast={onRetryLast}
                    onEditLastUser={onEditLastUser}
                  />
                </div>
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
