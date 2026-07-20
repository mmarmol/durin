import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ThreadActionsProvider } from "@/components/thread/ThreadActionsContext";
import { ThreadComposer } from "@/components/thread/ThreadComposer";
import { ThreadHeader } from "@/components/thread/ThreadHeader";
import { StreamErrorNotice } from "@/components/thread/StreamErrorNotice";
import { ThreadViewport } from "@/components/thread/ThreadViewport";
import { WorkPanel } from "@/components/work/WorkPanel";
import { WorkStrip } from "@/components/work/WorkStrip";
import type { OrbState } from "@/components/voice/VoiceOrb";
import { useDurinStream, type SendImage } from "@/hooks/useDurinStream";
import { useTranscriptionStatus } from "@/hooks/useTranscriptionStatus";
import { useModes } from "@/hooks/useModes";
import { useSessionHistory } from "@/hooks/useSessions";
import { useWorkState } from "@/hooks/useWorkState";
import { listSlashCommands, getModelCapabilities } from "@/lib/api";
import type { ChatSummary, SlashCommand, UIMessage } from "@/lib/types";
import { normalizeLegacyLongTaskMessages } from "@/lib/thread-display-compat";
import { scrubSubagentUiMessages } from "@/lib/subagent-channel-display";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function projectWebuiThreadMessages(messages: UIMessage[]): UIMessage[] {
  return scrubSubagentUiMessages(normalizeLegacyLongTaskMessages(messages));
}

interface ThreadShellProps {
  session: ChatSummary | null;
  title: string;
  onToggleSidebar: () => void;
  onGoHome?: () => void;
  onNewChat?: () => void;
  onCreateChat?: () => Promise<string | null>;
  onTurnEnd?: () => void;
  theme?: "light" | "dark";
  onToggleTheme?: () => void;
  hideSidebarToggleOnDesktop?: boolean;
  pendingPrompt?: string | null;
  onPromptConsumed?: () => void;
  /** Enter (or toggle off) hands-free voice mode from the composer orb. When
   *  omitted (voice unavailable) the composer hides the orb. */
  onEnterVoice?: () => void;
  voiceActive?: boolean;
  voiceState?: OrbState;
}

function toModelBadgeLabel(modelName: string | null): string | null {
  if (!modelName) return null;
  const trimmed = modelName.trim();
  if (!trimmed) return null;
  const leaf = trimmed.split("/").pop() ?? trimmed;
  return leaf || trimmed;
}

const EFFORT_VALUES = new Set(["none", "low", "medium", "high", "max"]);

// `/effort` encodes the reasoning level as a suffix on the active preset name
// (e.g. "default:high"). Return the effort the composer's picker should reflect,
// or null (Auto) when no effort suffix is set. Tracks the live runtime preset,
// so it refreshes on every model/effort switch.
function effortFromPreset(modelPreset: string | null): string | null {
  if (!modelPreset) return null;
  const tail = modelPreset.split(":").pop() ?? "";
  return EFFORT_VALUES.has(tail) ? tail : null;
}

interface PendingFirstMessage {
  content: string;
  images?: SendImage[];
}

export function ThreadShell({
  session,
  title,
  onToggleSidebar,
  onCreateChat,
  onTurnEnd,
  theme = "light",
  onToggleTheme = () => {},
  hideSidebarToggleOnDesktop = false,
  pendingPrompt = null,
  onPromptConsumed,
  onEnterVoice,
  voiceActive = false,
  voiceState = "idle",
}: ThreadShellProps) {
  const { t } = useTranslation();
  const chatId = session?.chatId ?? null;
  const historyKey = session?.key ?? null;
  // Non-websocket sessions (Telegram, CLI, subagent…) are view-only: the webui
  // can display their history but cannot continue them on a different channel.
  const isReadOnlyChannel = session !== null && session.channel !== "websocket";
  const [panelOpen, setPanelOpen] = useState(false);
  const work = useWorkState(chatId, historyKey);
  const {
    messages: historical,
    loading,
    hasPendingToolCalls,
    refresh: refreshHistory,
    version: historyVersion,
    persona: historicalPersona,
    prevCursor,
    loadingOlder,
    loadOlder: loadOlderHistory,
    adoptPrevCursor,
  } = useSessionHistory(historyKey);
  const { client, modelName, modelPreset, token } = useClient();
  const activeEffort = effortFromPreset(modelPreset);
  const [booting, setBooting] = useState(false);
  const [slashCommands, setSlashCommands] = useState<SlashCommand[]>([]);
  const [scrollToBottomSignal, setScrollToBottomSignal] = useState(0);
  const [canReason, setCanReason] = useState(false);
  const [localPendingPrompt, setLocalPendingPrompt] = useState<string | null>(null);
  const [agentMode, setAgentMode] = useState("build");
  const modes = useModes();
  const pendingFirstRef = useRef<PendingFirstMessage | null>(null);
  /** Per-chat snapshot of the live thread PLUS its older-history cursor.
   *  The cursor must travel with the rows: cached rows begin at the page
   *  boundary that was current when they were fetched, and after the session
   *  grows in the background a fresh refetch re-arms the hook's cursor at a
   *  LATER boundary — fetching older pages from that fresh cursor would
   *  overlap the cached rows by content under different ids. */
  const messageCacheRef = useRef<
    Map<string, { messages: UIMessage[]; prevCursor: number | null }>
  >(new Map());
  /** The live thread's older-history cursor for the CURRENT chat (kept in a
   *  ref so cache writes during a session switch can still record the
   *  outgoing chat's cursor). After a cache restore adopts the cached
   *  cursor into the hook, the hook's prevCursor and this ref agree. */
  const livePrevCursorRef = useRef<number | null>(null);
  /** Last chatId we associated with the in-memory thread (for cache-on-switch). */
  const prevChatIdForCacheRef = useRef<string | null>(null);
  /** Skip one message-cache write right after chatId changes (messages may not match yet). */
  const skipLayoutCacheRef = useRef(false);
  const appliedHistoryVersionRef = useRef<Map<string, number>>(new Map());
  const pendingCanonicalHydrateRef = useRef<Set<string>>(new Set());
  const sessionKeyByChatIdRef = useRef<Map<string, string>>(new Map());

  const initial = useMemo(() => {
    if (!chatId) return historical;
    return messageCacheRef.current.get(chatId)?.messages ?? historical;
  }, [chatId, historical]);
  const handleTurnEnd = useCallback(() => {
    onTurnEnd?.();
  }, [onTurnEnd]);
  const {
    messages,
    isStreaming,
    runStartedAt,
    goalState,
    send,
    stop,
    transcribeAudio,
    setMessages,
    streamError,
    dismissStreamError,
    apiStatus,
    dismissApiStatus,
  } = useDurinStream(chatId, initial, hasPendingToolCalls, handleTurnEnd);

  const transcriptionStatus = useTranscriptionStatus();

  // `historical` (from useSessionHistory) accumulates older pages as they're
  // fetched, but the live thread state (`messages`, from useDurinStream) only
  // seeds from `historical` at mount/chatId-change, so the loaded older rows
  // must be spliced into the live state here. Crucially, the merged list is
  // written to `messageCacheRef` inside the same updater: the
  // historical-watching resync effect below re-fires on this very
  // `historical` growth and — for a non-canonical change — restores the
  // cached snapshot, so the cache must already hold the merged list or that
  // restore would discard the page we just loaded. One merge, mirrored to
  // both stores the effect reads, keeps its overwrite a content no-op.
  const handleLoadOlder = useCallback(() => {
    void loadOlderHistory().then(({ rows: older, prevCursor: pageCursor }) => {
      if (older.length === 0) return;
      livePrevCursorRef.current = pageCursor;
      setMessages((prev) => {
        // Idempotent splice: after a session round-trip the live thread is
        // restored MERGED from the cache while the refetched history re-arms
        // prevCursor at the same offset, so a later scroll re-fetches a page
        // whose rows are already present — skip those instead of doubling them.
        const prevIds = new Set(prev.map((m) => m.id));
        const fresh = older.filter((m) => !prevIds.has(m.id));
        if (fresh.length === 0) return prev;
        const merged = [...fresh, ...prev];
        if (chatId) {
          messageCacheRef.current.set(chatId, {
            messages: projectWebuiThreadMessages(merged),
            prevCursor: pageCursor,
          });
        }
        return merged;
      });
    });
  }, [loadOlderHistory, setMessages, chatId]);

  // Track the live thread's cursor while the hook's state is authoritative
  // (fresh loads, canonical hydrates, older-page fetches). Declared BEFORE
  // the resync effect below so cache restores — which overwrite this ref
  // directly — see a fresh value on their own pass.
  useEffect(() => {
    if (!loading) livePrevCursorRef.current = prevCursor;
  }, [loading, prevCursor]);

  useEffect(() => {
    if (chatId && historyKey) sessionKeyByChatIdRef.current.set(chatId, historyKey);
  }, [chatId, historyKey]);

  const displayMessages = useMemo(() => projectWebuiThreadMessages(messages), [messages]);

  const showHeroComposer = messages.length === 0 && !loading;

  useEffect(() => {
    if (!chatId || loading) return;
    const cached = messageCacheRef.current.get(chatId);
    const appliedVersion = appliedHistoryVersionRef.current.get(chatId) ?? 0;
    const hasPendingCanonicalHydrate = pendingCanonicalHydrateRef.current.has(chatId);
    const hasNewCanonicalHistory = hasPendingCanonicalHydrate && historyVersion > appliedVersion;
    // Restoring from cache restores the pagination cursor WITH the snapshot:
    // the cached rows begin at the boundary that was current when they were
    // fetched, and if the session grew in the background the fresh refetch's
    // cursor points later — older pages must chain from the cached boundary
    // so the next page ends exactly where the restored rows begin.
    const willUseCanonical = hasNewCanonicalHistory && historical.length > 0;
    const restoreEntry =
      !willUseCanonical && cached && cached.messages.length > 0 ? cached : null;
    if (restoreEntry) {
      livePrevCursorRef.current = restoreEntry.prevCursor;
      adoptPrevCursor(restoreEntry.prevCursor);
    }
    // When the user switches away and back, keep the local in-memory thread
    // state (including not-yet-persisted messages) instead of replacing it with
    // whatever the history endpoint currently knows about. Once a fresh
    // canonical replay arrives (e.g. after ``session_updated`` refresh), prefer it
    // so rendering converges to the same shape as a manual refresh.
    setMessages((prev) => {
      if (hasNewCanonicalHistory && historical.length > 0) {
        pendingCanonicalHydrateRef.current.delete(chatId);
        appliedHistoryVersionRef.current.set(chatId, historyVersion);
        // Canonical rows arrive without the client-only ``renderKey``; inherit
        // it so React keeps the DOM subtree mounted (no iframe reload / toggle
        // reset) across the hydration swap. Two lookups: by id for rows the
        // server stamped, then by role+content for streamed replies — those
        // live under a placeholder uuid that canonical replay never reuses.
        const rawNormalized = projectWebuiThreadMessages(historical);
        const prevProjected = projectWebuiThreadMessages(prev);
        const prevRenderKeys = new Map(
          prevProjected.filter((m) => m.renderKey).map((m) => [m.id, m.renderKey as string]),
        );
        const rawCanonicalIds = new Set(rawNormalized.map((m) => m.id));
        // Pool of live rows about to be dropped by this swap — each may donate
        // its render identity to exactly one canonical row (consume-once, so
        // duplicate contents can't mint duplicate React keys).
        const donorPool = prevProjected.filter((m) => !rawCanonicalIds.has(m.id));
        const normalized = rawNormalized.map((m) => {
          const byId = prevRenderKeys.get(m.id);
          if (byId) return { ...m, renderKey: byId };
          const donorIdx = donorPool.findIndex(
            (d) => d.role === m.role && d.kind === m.kind && d.content === m.content,
          );
          if (donorIdx === -1) return m;
          const donor = donorPool.splice(donorIdx, 1)[0];
          return { ...m, renderKey: donor.renderKey ?? donor.id };
        });
        // Canonical replay is authoritative. The only live row it can legitimately
        // be missing is a server-stamped command output (id ``msg-…``) whose
        // persistence the refetch raced ahead of — command turns emit no
        // ``turn_end``, so the live row was never re-anchored. Keep ONLY those.
        // Every other live row (notably a streamed reply, keyed by a fallback
        // ``crypto.randomUUID()`` and persisted under a different replay id) is
        // already represented in canonical; re-appending it would render it
        // twice, so it must be dropped here.
        const canonicalIds = new Set(normalized.map((m) => m.id));
        const liveOnly = projectWebuiThreadMessages(prev).filter(
          (m) => m.id.startsWith("msg-") && !canonicalIds.has(m.id),
        );
        const merged = [...normalized, ...liveOnly];
        messageCacheRef.current.set(chatId, {
          messages: merged,
          prevCursor: livePrevCursorRef.current,
        });
        return merged;
      }
      if (restoreEntry) return projectWebuiThreadMessages(restoreEntry.messages);
      if (historical.length === 0 && prev.length > 0) return projectWebuiThreadMessages(prev);
      appliedHistoryVersionRef.current.set(chatId, historyVersion);
      const next = projectWebuiThreadMessages(historical);
      if (historical.length > 0) {
        messageCacheRef.current.set(chatId, {
          messages: next,
          prevCursor: livePrevCursorRef.current,
        });
      }
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, chatId, historical, historyVersion]);

  useEffect(() => {
    if (!chatId) return;
    return client.onSessionUpdate((updatedChatId) => {
      if (updatedChatId !== chatId) return;
      pendingCanonicalHydrateRef.current.add(chatId);
      refreshHistory();
    });
  }, [chatId, client, refreshHistory]);

  // Keyed on `historyVersion`, not `historical`: the version bumps only when
  // a full (re)load completes, while an older-page prepend grows `historical`
  // without touching it — re-arming the scroll-to-bottom signal for a prepend
  // would yank the view away from the history the user just scrolled up to.
  useEffect(() => {
    if (!chatId || loading) return;
    setScrollToBottomSignal((value) => value + 1);
  }, [chatId, loading, historyVersion]);

  useEffect(() => {
    if (chatId) return;
    setMessages(projectWebuiThreadMessages(historical));
  }, [chatId, historical, setMessages]);

  useLayoutEffect(() => {
    // At this commit the hook already reset for the incoming key, so
    // livePrevCursorRef still holds the OUTGOING chat's cursor — exactly
    // what its snapshot must record.
    if (chatId) {
      const prev = prevChatIdForCacheRef.current;
      if (prev && prev !== chatId) {
        messageCacheRef.current.set(prev, {
          messages: projectWebuiThreadMessages(messages),
          prevCursor: livePrevCursorRef.current,
        });
        skipLayoutCacheRef.current = true;
      }
      prevChatIdForCacheRef.current = chatId;
    } else {
      if (prevChatIdForCacheRef.current) {
        messageCacheRef.current.set(prevChatIdForCacheRef.current, {
          messages: projectWebuiThreadMessages(messages),
          prevCursor: livePrevCursorRef.current,
        });
        skipLayoutCacheRef.current = true;
      }
      prevChatIdForCacheRef.current = null;
    }
  }, [chatId, messages]);

  // Persist thread to in-memory cache after paint so ``useDurinStream``'s chat switch
  // ``useEffect`` reset has flushed; ``skipLayoutCacheRef`` drops the first run that still
  // sees the *previous* chat's ``messages`` (avoids stale rows leaking across sessions).
  useEffect(() => {
    if (!chatId) {
      return;
    }
    if (skipLayoutCacheRef.current) {
      skipLayoutCacheRef.current = false;
      return;
    }
    if (loading) {
      return;
    }
    messageCacheRef.current.set(chatId, {
      messages: projectWebuiThreadMessages(messages),
      prevCursor: livePrevCursorRef.current,
    });
  }, [chatId, loading, messages]);

  useEffect(() => {
    if (!chatId) return;
    const pending = pendingFirstRef.current;
    if (!pending) return;
    pendingFirstRef.current = null;
    setScrollToBottomSignal((value) => value + 1);
    send(pending.content, pending.images);
    setBooting(false);
  }, [chatId, send]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const commands = await listSlashCommands(token);
        if (!cancelled) setSlashCommands(commands);
      } catch {
        if (!cancelled) setSlashCommands([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    if (!modelName) return;
    let cancelled = false;
    getModelCapabilities(token, modelName, "")
      .then((caps) => {
        if (!cancelled) setCanReason(!!caps.supports_reasoning);
      })
      .catch(() => {
        if (!cancelled) setCanReason(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, modelName]);

  const handleWelcomeSend = useCallback(
    async (content: string, images?: SendImage[]) => {
      if (booting) return;
      setBooting(true);
      pendingFirstRef.current = { content, images };
      const newId = await onCreateChat?.();
      if (!newId) {
        pendingFirstRef.current = null;
        setBooting(false);
      }
    },
    [booting, onCreateChat],
  );

  const handleThreadSend = useCallback(
    (content: string, images?: SendImage[], opts?: { steer?: boolean }) => {
      setScrollToBottomSignal((value) => value + 1);
      send(content, images, opts);
    },
    [send],
  );

  const handleRetryLast = useCallback(() => {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (lastUser && chatId) {
      setScrollToBottomSignal((v) => v + 1);
      send(lastUser.content);
    }
  }, [messages, chatId, send]);

  const handleEditLastUser = useCallback(() => {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (lastUser) {
      setLocalPendingPrompt(lastUser.content);
    }
  }, [messages]);

  const handleModeChange = useCallback(
    (mode: string) => {
      setAgentMode(mode);
      const cid = chatId ?? client.defaultChatId;
      if (cid) client.sendMessage(cid, `/mode ${mode}`);
    },
    [chatId, client],
  );

  const [activePersona, setActivePersona] = useState<string | null>(null);
  // Seed the pill from the fetched thread payload whenever the active chat changes.
  useEffect(() => {
    setActivePersona(historicalPersona ?? null);
  }, [historyKey, historicalPersona]);
  const handlePersonaPick = useCallback(
    (name: string) => {
      setActivePersona(name);
      const cid = chatId ?? client.defaultChatId;
      if (cid) client.sendMessage(cid, `/persona ${name}`);
    },
    [chatId, client],
  );

  // Lets an interaction block deep in the transcript answer a question
  // or store a requested secret without drilling callbacks through
  // viewport → list → bubble.
  const threadActions = useMemo(
    () => ({
      sendUserMessage: (text: string) => handleThreadSend(text),
      storeSecret: (input: {
        name: string;
        service?: string;
        value: string;
        scope?: string[];
        rotate?: boolean;
      }) => client.storeSecret({ ...input, chatId: chatId ?? undefined }),
      openWorkPanel: () => setPanelOpen(true),
    }),
    [handleThreadSend, client, chatId],
  );

  const handleModelPick = useCallback(
    (ref: string) => {
      const cid = chatId ?? client.defaultChatId;
      if (cid) {
        // `ref` is the exact `/model` argument: "default", a preset name, or
        // "provider model" — committed verbatim, no client-side inference.
        client.sendMessage(cid, `/model ${ref}`);
      }
    },
    [chatId, client],
  );

  const handleEffortPick = useCallback(
    (effort: string) => {
      const cid = chatId ?? client.defaultChatId;
      if (cid) {
        client.sendMessage(cid, `/effort ${effort}`);
      }
    },
    [chatId, client],
  );

  const readOnlyBanner = isReadOnlyChannel ? (
    <div
      role="status"
      className={cn(
        "mb-2 rounded-lg border border-border/50 bg-muted/60 px-3 py-2",
        "text-[12px] leading-5 text-muted-foreground text-center",
      )}
    >
      {t("chat.readOnlyChannel", { channel: session!.channel })}
    </div>
  ) : null;

  const composer = (
    <>
      {streamError ? (
        <StreamErrorNotice
          error={streamError}
          onDismiss={dismissStreamError}
        />
      ) : null}
      {readOnlyBanner}
      {session && !panelOpen ? (
        // Same centering constraint as the composer root (58rem hero / 49.5rem
        // thread) so the strip aligns with the input instead of spanning the
        // viewport's full content width.
        <div
          className={cn(
            "mx-auto w-full",
            showHeroComposer ? "max-w-[58rem]" : "max-w-[49.5rem]",
          )}
        >
          <WorkStrip
            key={chatId ?? "no-chat"}
            active={work.active}
            finished={work.finished}
            onOpen={() => setPanelOpen(true)}
          />
        </div>
      ) : null}
      {session ? (
        <ThreadComposer
          onSend={handleThreadSend}
          onTranscribeAudio={transcribeAudio}
          audioInputAllowed={transcriptionStatus.available}
          disabled={!chatId || isReadOnlyChannel}
          isStreaming={isStreaming}
          placeholder={
            showHeroComposer
              ? t("thread.composer.placeholderHero")
              : t("thread.composer.placeholderThread")
          }
          modelLabel={toModelBadgeLabel(modelName)}
          variant={showHeroComposer ? "hero" : "thread"}
          slashCommands={slashCommands}
          onStop={stop}
          runStartedAt={runStartedAt}
          goalState={goalState}
          onModelPick={handleModelPick}
          onEffortPick={handleEffortPick}
          activeEffort={activeEffort}
          canReason={canReason}
          pendingPrompt={pendingPrompt ?? localPendingPrompt}
          onPromptConsumed={() => { onPromptConsumed?.(); setLocalPendingPrompt(null); }}
          modes={modes}
          agentMode={agentMode}
          onModeChange={handleModeChange}
          onEnterVoice={onEnterVoice}
          voiceActive={voiceActive}
          voiceState={voiceState}
          apiStatus={apiStatus}
          onDismissApiStatus={dismissApiStatus}
          onPersonaPick={handlePersonaPick}
          activePersona={activePersona}
        />
      ) : (
        <ThreadComposer
          onSend={handleWelcomeSend}
          onTranscribeAudio={transcribeAudio}
          audioInputAllowed={transcriptionStatus.available}
          disabled={booting}
          isStreaming={isStreaming}
          placeholder={
            booting
              ? t("thread.composer.placeholderOpening")
              : t("thread.composer.placeholderHero")
          }
          modelLabel={toModelBadgeLabel(modelName)}
          variant="hero"
          slashCommands={slashCommands}
          runStartedAt={runStartedAt}
          goalState={goalState}
          onModelPick={handleModelPick}
          onEffortPick={handleEffortPick}
          activeEffort={activeEffort}
          canReason={canReason}
          pendingPrompt={pendingPrompt ?? localPendingPrompt}
          onPromptConsumed={() => { onPromptConsumed?.(); setLocalPendingPrompt(null); }}
          modes={modes}
          agentMode={agentMode}
          onModeChange={handleModeChange}
          onEnterVoice={onEnterVoice}
          voiceActive={voiceActive}
          voiceState={voiceState}
          apiStatus={apiStatus}
          onDismissApiStatus={dismissApiStatus}
          onPersonaPick={handlePersonaPick}
          activePersona={activePersona}
        />
      )}
    </>
  );

  const emptyState = loading ? (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      {t("thread.loadingConversation")}
    </div>
  ) : (
    <div className="flex w-full flex-col items-center text-center animate-in fade-in-0 slide-in-from-bottom-2 duration-500">
      <h1 className="text-balance text-[40px] font-normal leading-tight tracking-[-0.045em] text-foreground sm:text-[48px]">
        {t("thread.empty.greeting")}
      </h1>
    </div>
  );

  return (
    <ThreadActionsProvider value={threadActions}>
    <section className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
      <ThreadHeader
        title={title}
        onToggleSidebar={onToggleSidebar}
        theme={theme}
        onToggleTheme={onToggleTheme}
        hideSidebarToggleOnDesktop={hideSidebarToggleOnDesktop}
        minimal={!session && !loading}
        onTogglePanel={session ? () => setPanelOpen((o) => !o) : undefined}
        panelOpen={panelOpen}
        panelHasActiveWork={work.active.length > 0}
      />
      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          <ThreadViewport
            messages={displayMessages}
            isStreaming={isStreaming}
            emptyState={emptyState}
            composer={composer}
            scrollToBottomSignal={scrollToBottomSignal}
            conversationKey={historyKey}
            onRetryLast={handleRetryLast}
            onEditLastUser={handleEditLastUser}
            onLoadOlder={handleLoadOlder}
            hasOlder={prevCursor != null}
            loadingOlder={loadingOlder}
          />
        </div>
        <WorkPanel
          active={work.active}
          finished={work.finished}
          open={panelOpen}
          onClose={() => setPanelOpen(false)}
        />
      </div>
    </section>
    </ThreadActionsProvider>
  );
}
