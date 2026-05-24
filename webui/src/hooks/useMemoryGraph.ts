import { useCallback, useEffect, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import {
  ApiError,
  fetchMemoryGraph,
  type MemoryGraphPayload,
} from "@/lib/api";

/** Loads the entity-centric memory graph for the Obsidian-style view.
 *
 * Refresh is manual via `refresh()` — the graph builder walks disk on
 * every call, so we don't auto-poll. The caller decides when to refetch
 * (typically: on view mount, after a dream pass, on user click). */
export function useMemoryGraph(enabled: boolean): {
  data: MemoryGraphPayload | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
} {
  const { token } = useClient();
  const tokenRef = useRef(token);
  tokenRef.current = token;
  const [data, setData] = useState<MemoryGraphPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!tokenRef.current) return;
    try {
      setLoading(true);
      setError(null);
      const payload = await fetchMemoryGraph(tokenRef.current);
      setData(payload);
    } catch (e) {
      const msg = e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void refresh();
  }, [enabled, refresh]);

  return { data, loading, error, refresh };
}
