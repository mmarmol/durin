import { useEffect, useRef, useState } from "react";

import { fetchWebuiThread } from "@/lib/api";
import type { UIMessage } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

export type NodeTranscriptState = "idle" | "loading" | "missing";

/** How often an in-flight node's record is re-read. A node checkpoints its
 *  conversation once per agent round — rounds take seconds at best — and the
 *  server caches the conversion by session-file identity, so re-asking for an
 *  unchanged session costs next to nothing. */
const LIVE_POLL_MS = 3_000;

/**
 * One workflow node's conversation, rendered read-only by the server.
 *
 * There is no stream to subscribe to: the node persists its conversation after
 * every agent round, so re-reading it IS the live view — the same call serves a
 * node that finished last week and one that is three rounds into its turn.
 */
export function useNodeTranscript(
  sessionKey: string | null,
  live: boolean,
): { messages: UIMessage[] | null; state: NodeTranscriptState } {
  const { token } = useClient();
  const [messages, setMessages] = useState<UIMessage[] | null>(null);
  const [state, setState] = useState<NodeTranscriptState>("idle");
  // Survives re-renders so a poll landing after the panel closed — or after the
  // user moved to another node — cannot write a stale transcript into state.
  const currentKey = useRef<string | null>(null);

  useEffect(() => {
    currentKey.current = sessionKey;
    if (!sessionKey || !token) {
      setMessages(null);
      setState("idle");
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const read = async (first: boolean) => {
      if (first) setState("loading");
      try {
        const payload = await fetchWebuiThread(token, sessionKey);
        if (cancelled || currentKey.current !== sessionKey) return;
        if (payload == null) {
          setMessages(null);
          setState("missing");
        } else {
          setMessages(payload.messages ?? []);
          setState("idle");
        }
      } catch {
        if (cancelled || currentKey.current !== sessionKey) return;
        // A node whose session cannot be read is reported as missing rather than
        // as an empty transcript: "it did nothing" and "we cannot see what it
        // did" are different answers.
        setState("missing");
      }
      if (!cancelled && live) timer = setTimeout(() => void read(false), LIVE_POLL_MS);
    };

    void read(true);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [sessionKey, token, live]);

  return { messages, state };
}
