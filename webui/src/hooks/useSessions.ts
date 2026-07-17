import { useCallback, useEffect, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import i18n from "@/i18n";
import {
  ApiError,
  deleteSession as apiDeleteSession,
  fetchWebuiThread,
  listSessions,
  renameSession as apiRenameSession,
} from "@/lib/api";
import { deriveTitle } from "@/lib/format";
import type { ChatSummary, UIMessage } from "@/lib/types";

const EMPTY_MESSAGES: UIMessage[] = [];

/** Sidebar state: fetches the full session list and exposes create / delete actions. */
export function useSessions(): {
  sessions: ChatSummary[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  createChat: () => Promise<string>;
  deleteChat: (key: string) => Promise<void>;
  renameChat: (key: string, title: string) => Promise<void>;
} {
  const { client, token } = useClient();
  const [sessions, setSessions] = useState<ChatSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const tokenRef = useRef(token);
  tokenRef.current = token;

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const rows = await listSessions(tokenRef.current);
      setSessions(rows);
      setError(null);
    } catch (e) {
      const msg =
        e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    return client.onSessionUpdate(() => {
      void refresh();
    });
  }, [client, refresh]);

  const createChat = useCallback(async (): Promise<string> => {
    const chatId = await client.newChat();
    const key = `websocket:${chatId}`;
    // Optimistic insert; a subsequent refresh will replace it with the
    // authoritative row once the server persists the session.
    setSessions((prev) => [
      {
        key,
        channel: "websocket",
        chatId,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        title: "",
        preview: "",
      },
      ...prev.filter((s) => s.key !== key),
    ]);
    return chatId;
  }, [client]);

  const deleteChat = useCallback(
    async (key: string) => {
      await apiDeleteSession(tokenRef.current, key);
      setSessions((prev) => prev.filter((s) => s.key !== key));
    },
    [],
  );

  const renameChat = useCallback(
    async (key: string, title: string) => {
      const persisted = await apiRenameSession(tokenRef.current, key, title);
      // Optimistic — server returns the normalized (trimmed/capped) title.
      // Keep the sidebar row order stable; only the label changes.
      setSessions((prev) =>
        prev.map((s) => (s.key === key ? { ...s, title: persisted } : s)),
      );
    },
    [],
  );

  return { sessions, loading, error, refresh, createChat, deleteChat, renameChat };
}

/** Lazy-load a session's on-disk messages the first time the UI displays it. */
export function useSessionHistory(key: string | null): {
  messages: UIMessage[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
  version: number;
  /** ``true`` when the replayed transcript ends with a trace row (turn still in flight). */
  hasPendingToolCalls: boolean;
  /** Active persona slug for this session, or null if none is set. */
  persona: string | null;
  /** Byte cursor for the next older page; ``null`` once history's start is reached. */
  prevCursor: number | null;
  /** ``true`` while an older page fetch (triggered by ``loadOlder``) is in flight. */
  loadingOlder: boolean;
  /** Fetch and prepend the next older page. No-op if already at the start of
   *  history or a fetch is already in flight. Resolves with the prepended
   *  rows so callers can splice them into any separately-tracked live thread. */
  loadOlder: () => Promise<UIMessage[]>;
} {
  const { token } = useClient();
  const [refreshSeq, setRefreshSeq] = useState(0);
  const refresh = useCallback(() => {
    setRefreshSeq((value) => value + 1);
  }, []);
  const [state, setState] = useState<{
    key: string | null;
    messages: UIMessage[];
    loading: boolean;
    error: string | null;
    hasPendingToolCalls: boolean;
    version: number;
    persona: string | null;
    prevCursor: number | null;
    loadingOlder: boolean;
  }>({
    key: null,
    messages: [],
    loading: false,
    error: null,
    hasPendingToolCalls: false,
    version: 0,
    persona: null,
    prevCursor: null,
    loadingOlder: false,
  });
  const loadingOlderRef = useRef(false);
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    if (!key) {
      setState({
        key: null,
        messages: [],
        loading: false,
        error: null,
        hasPendingToolCalls: false,
        version: 0,
        persona: null,
        prevCursor: null,
        loadingOlder: false,
      });
      return;
    }
    let cancelled = false;
    // Mark the new key as loading immediately so callers never see stale
    // messages from the previous session during the render right after a switch.
    setState((prev) => prev.key === key
      ? { ...prev, loading: true, error: null }
      : {
          key,
          messages: [],
          loading: true,
          error: null,
          hasPendingToolCalls: false,
          version: 0,
          persona: null,
          prevCursor: null,
          loadingOlder: false,
        });
    (async () => {
      try {
        const body = await fetchWebuiThread(token, key);
        if (cancelled) return;
        if (!body?.messages?.length) {
          setState((prev) => ({
            key,
            messages: [],
            loading: false,
            error: null,
            hasPendingToolCalls: false,
            version: prev.key === key ? prev.version + 1 : 1,
            persona: body?.persona ?? null,
            prevCursor: body?.prevCursor ?? null,
            loadingOlder: false,
          }));
          return;
        }
        // The newest page is a singleton per session (there's exactly one
        // "current" fetch with no `before`); a non-numeric sentinel keeps its
        // ids stable and can never collide with an older page's ids, which
        // always embed the numeric byte cursor used to fetch them.
        const ui: UIMessage[] = body.messages.map((m, idx) => ({
          ...m,
          id: m.id ?? `hist-first-${idx}`,
          createdAt: typeof m.createdAt === "number" ? m.createdAt : Date.now(),
        }));
        const last = ui[ui.length - 1];
        const hasPending = last?.kind === "trace";
        setState((prev) => ({
          key,
          messages: ui,
          loading: false,
          error: null,
          hasPendingToolCalls: hasPending,
          version: prev.key === key ? prev.version + 1 : 1,
          persona: body.persona ?? null,
          prevCursor: body.prevCursor ?? null,
          loadingOlder: false,
        }));
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          setState((prev) => ({
            key,
            messages: [],
            loading: false,
            error: null,
            hasPendingToolCalls: false,
            version: prev.key === key ? prev.version + 1 : 1,
            persona: null,
            prevCursor: null,
            loadingOlder: false,
          }));
        } else {
          setState((prev) => ({
            key,
            messages: [],
            loading: false,
            error: (e as Error).message,
            hasPendingToolCalls: false,
            version: prev.key === key ? prev.version : 0,
            persona: null,
            prevCursor: null,
            loadingOlder: false,
          }));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [key, token, refreshSeq]);

  const loadOlder = useCallback(async (): Promise<UIMessage[]> => {
    const current = stateRef.current;
    if (!key || current.key !== key || current.prevCursor == null || loadingOlderRef.current) {
      return [];
    }
    const before = current.prevCursor;
    loadingOlderRef.current = true;
    setState((prev) => (prev.key === key ? { ...prev, loadingOlder: true } : prev));
    try {
      const body = await fetchWebuiThread(token, key, "", before);
      if (stateRef.current.key !== key) return [];
      const older: UIMessage[] = (body?.messages ?? []).map((m, idx) => ({
        ...m,
        id: m.id ?? `hist-${before}-${idx}`,
        createdAt: typeof m.createdAt === "number" ? m.createdAt : Date.now(),
      }));
      setState((prev) =>
        prev.key === key
          ? {
              ...prev,
              messages: [...older, ...prev.messages],
              prevCursor: body?.prevCursor ?? null,
              loadingOlder: false,
            }
          : prev,
      );
      return older;
    } catch {
      setState((prev) => (prev.key === key ? { ...prev, loadingOlder: false } : prev));
      return [];
    } finally {
      loadingOlderRef.current = false;
    }
  }, [key, token]);

  if (!key) {
    return {
      messages: EMPTY_MESSAGES,
      loading: false,
      error: null,
      refresh,
      version: 0,
      hasPendingToolCalls: false,
      persona: null,
      prevCursor: null,
      loadingOlder: false,
      loadOlder,
    };
  }

  // Even before the effect above commits its loading state, never surface the
  // previous session's payload for a brand-new key.
  if (state.key !== key) {
    return {
      messages: EMPTY_MESSAGES,
      loading: true,
      error: null,
      refresh,
      version: 0,
      hasPendingToolCalls: false,
      persona: null,
      prevCursor: null,
      loadingOlder: false,
      loadOlder,
    };
  }

  return {
    messages: state.messages,
    loading: state.loading,
    error: state.error,
    refresh,
    version: state.version,
    hasPendingToolCalls: state.hasPendingToolCalls,
    persona: state.persona,
    prevCursor: state.prevCursor,
    loadingOlder: state.loadingOlder,
    loadOlder,
  };
}

/** Produce a compact display title for a session. */
export function sessionTitle(
  session: ChatSummary,
  firstUserMessage?: string,
): string {
  return deriveTitle(
    session.title || firstUserMessage || session.preview,
    i18n.t("chat.newChat"),
  );
}
