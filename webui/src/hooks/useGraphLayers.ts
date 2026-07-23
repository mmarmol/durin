import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchClusterSubgraph,
  fetchMemoryGraphOverview,
  fetchMemorySubgraph,
  type MemoryGraphPayload,
  type MemoryOverviewPayload,
} from "@/lib/api";

export type GraphLayer =
  | { kind: "overview" }
  | { kind: "cluster"; ref: string; name: string }
  | { kind: "ego"; ref: string; name: string };

export interface GraphLayers {
  layer: GraphLayer;
  overview: MemoryOverviewPayload | null;
  focusGraph: MemoryGraphPayload | null;
  totalMembers: number | null;
  loading: boolean;
  error: string | null;
  notice: "staleCluster" | null;
  enterCluster(ref: string, name: string): Promise<void>;
  enterEgo(ref: string, name: string, hops?: number): Promise<void>;
  backToOverview(): void;
  refreshOverview(): Promise<boolean>;
}

export function useGraphLayers(
  enabled: boolean,
  getToken: () => string | null,
  groupBy: "community" | "type" = "community",
): GraphLayers {
  const [layer, setLayer] = useState<GraphLayer>({ kind: "overview" });
  const [overview, setOverview] = useState<MemoryOverviewPayload | null>(null);
  const [focusGraph, setFocusGraph] = useState<MemoryGraphPayload | null>(null);
  const [totalMembers, setTotalMembers] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<"staleCluster" | null>(null);
  const overviewJson = useRef<string | null>(null);
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  // The groupBy this hook last fetched under — lets the effect below tell a
  // genuine dimension switch apart from a same-dimension refresh (both reach
  // loadOverview through the same effect/callback identity change).
  const prevGroupByRef = useRef(groupBy);

  const loadOverview = useCallback(async (): Promise<boolean> => {
    const token = getTokenRef.current();
    if (token == null) return false;
    setLoading(true);
    try {
      const payload = await fetchMemoryGraphOverview(token, undefined, groupBy);
      // Fingerprint the whole payload, not just stats+mode — the overview
      // is bounded (~100 elements), so stringifying it in full is cheap and
      // catches any change (bubble membership, edges, ...) that stats alone
      // would miss.
      const json = JSON.stringify(payload);
      const prior = overviewJson.current;
      const changed = prior !== null && prior !== json;
      overviewJson.current = json;
      // Only replace the overview object when the payload actually differs —
      // an identical refresh must not hand downstream consumers a new
      // reference, or the sim-node rebuild it triggers reheats the layout
      // and wipes any positions the user has pinned.
      if (prior === null || prior !== json) {
        setOverview(payload);
      }
      setError(null);
      return changed;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setLoading(false);
    }
  }, [groupBy]);

  // Re-fires whenever `groupBy` changes: a fresh `loadOverview` identity
  // (it depends on `groupBy`, above) makes this effect re-run even though
  // `enabled` itself didn't change. A genuine dimension switch (not the
  // initial mount, and not a same-dimension refreshOverview() call) throws
  // away the previous dimension's overview *before* kicking off the new
  // fetch — first-load semantics — so the view shows its loading state
  // instead of the wrong dimension's map while the new one is in flight.
  // `overviewJson` resets alongside it so the resolved payload lands as a
  // first load rather than a same-dimension refresh, which would otherwise
  // preserve the old object reference (see the identity guard above) even
  // though it now describes a different dimension.
  useEffect(() => {
    if (prevGroupByRef.current !== groupBy) {
      prevGroupByRef.current = groupBy;
      setOverview(null);
      overviewJson.current = null;
    }
    if (enabled) void loadOverview();
  }, [enabled, loadOverview, groupBy]);

  const backToOverview = useCallback(() => {
    setLayer({ kind: "overview" });
    setFocusGraph(null);
    setTotalMembers(null);
    setError(null);
  }, []);

  const enterCluster = useCallback(
    async (ref: string, name: string) => {
      const token = getTokenRef.current();
      if (token == null) return;
      setLoading(true);
      setNotice(null);
      try {
        const payload = await fetchClusterSubgraph(token, ref, undefined, groupBy);
        if (payload === null) {
          backToOverview();
          setNotice("staleCluster");
          void loadOverview();
          return;
        }
        setFocusGraph(payload);
        setTotalMembers(payload.total_members);
        setLayer({ kind: "cluster", ref, name });
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [backToOverview, loadOverview, groupBy],
  );

  const enterEgo = useCallback(
    async (ref: string, name: string, hops = 1) => {
      const token = getTokenRef.current();
      if (token == null) return;
      setLoading(true);
      setNotice(null);
      try {
        const payload = await fetchMemorySubgraph(token, ref, { hops });
        setFocusGraph(payload);
        setTotalMembers(null);
        setLayer({ kind: "ego", ref, name });
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  return {
    layer,
    overview,
    focusGraph,
    totalMembers,
    loading,
    error,
    notice,
    enterCluster,
    enterEgo,
    backToOverview,
    refreshOverview: loadOverview,
  };
}
