import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = {
  fetchMemoryGraphOverview: vi.fn(),
  fetchClusterSubgraph: vi.fn(),
  fetchMemorySubgraph: vi.fn(),
};
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchMemoryGraphOverview: (...a: unknown[]) => api.fetchMemoryGraphOverview(...a),
  fetchClusterSubgraph: (...a: unknown[]) => api.fetchClusterSubgraph(...a),
  fetchMemorySubgraph: (...a: unknown[]) => api.fetchMemorySubgraph(...a),
}));

import { useGraphLayers } from "@/hooks/useGraphLayers";

const OVERVIEW = {
  mode: "clustered", bubbles: [{ id: "topic:m0", name: "m0", count: 17, types: ["topic"], top: [] }],
  hubs: [], loose: [], edges: [],
  stats: { entity_count: 17, reference_count: 0, bubble_count: 1, loose_count: 0, phantom_count: 3, session_count: 2 },
};
const CLUSTER = { nodes: [], edges: [], stats: { node_count: 0, edge_count: 0, phantom_count: 0, truncated_nodes: false, truncated_edges: false, types: [] }, focus: "topic:m0", total_members: 17 };

describe("useGraphLayers", () => {
  beforeEach(() => {
    api.fetchMemoryGraphOverview.mockReset().mockResolvedValue(OVERVIEW);
    api.fetchClusterSubgraph.mockReset().mockResolvedValue(CLUSTER);
    api.fetchMemorySubgraph.mockReset().mockResolvedValue({ ...CLUSTER, focus: "person:a" });
  });

  it("fetches the overview when enabled", async () => {
    const { result } = renderHook(() => useGraphLayers(true, () => "tok"));
    await waitFor(() => expect(result.current.overview).not.toBeNull());
    expect(result.current.layer.kind).toBe("overview");
  });

  it("enterCluster drills and backToOverview returns", async () => {
    const { result } = renderHook(() => useGraphLayers(true, () => "tok"));
    await waitFor(() => expect(result.current.overview).not.toBeNull());
    await act(() => result.current.enterCluster("topic:m0", "m0"));
    expect(result.current.layer).toEqual({ kind: "cluster", ref: "topic:m0", name: "m0" });
    expect(result.current.focusGraph).not.toBeNull();
    expect(result.current.totalMembers).toBe(17);
    act(() => result.current.backToOverview());
    expect(result.current.layer.kind).toBe("overview");
    expect(result.current.focusGraph).toBeNull();
  });

  it("stale cluster (404→null) falls back to overview with a notice", async () => {
    api.fetchClusterSubgraph.mockResolvedValue(null);
    const { result } = renderHook(() => useGraphLayers(true, () => "tok"));
    await waitFor(() => expect(result.current.overview).not.toBeNull());
    await act(() => result.current.enterCluster("topic:gone", "gone"));
    expect(result.current.layer.kind).toBe("overview");
    expect(result.current.notice).toBe("staleCluster");
  });

  it("a failed overview refresh keeps the last good overview", async () => {
    const { result } = renderHook(() => useGraphLayers(true, () => "tok"));
    await waitFor(() => expect(result.current.overview).not.toBeNull());
    api.fetchMemoryGraphOverview.mockRejectedValue(new Error("boom"));
    await act(async () => { await result.current.refreshOverview(); });
    expect(result.current.overview).not.toBeNull();
    expect(result.current.error).toBeTruthy();
  });

  it("refreshOverview reports whether the payload changed", async () => {
    const { result } = renderHook(() => useGraphLayers(true, () => "tok"));
    await waitFor(() => expect(result.current.overview).not.toBeNull());
    let changed = true;
    await act(async () => { changed = await result.current.refreshOverview(); });
    expect(changed).toBe(false);
    api.fetchMemoryGraphOverview.mockResolvedValue({ ...OVERVIEW, stats: { ...OVERVIEW.stats, entity_count: 99 } });
    await act(async () => { changed = await result.current.refreshOverview(); });
    expect(changed).toBe(true);
  });

  it("enterEgo drills into the ego neighborhood", async () => {
    const { result } = renderHook(() => useGraphLayers(true, () => "tok"));
    await waitFor(() => expect(result.current.overview).not.toBeNull());
    await act(() => result.current.enterEgo("person:a", "A"));
    expect(result.current.layer).toEqual({ kind: "ego", ref: "person:a", name: "A" });
    expect(result.current.focusGraph).not.toBeNull();
    expect(result.current.totalMembers).toBeNull();
  });

  it("a failed drill keeps the current layer and sets error", async () => {
    api.fetchClusterSubgraph.mockRejectedValue(new Error("net down"));
    api.fetchMemorySubgraph.mockRejectedValue(new Error("net down"));
    const { result } = renderHook(() => useGraphLayers(true, () => "tok"));
    await waitFor(() => expect(result.current.overview).not.toBeNull());
    await act(() => result.current.enterCluster("topic:m0", "m0"));
    expect(result.current.layer.kind).toBe("overview");
    expect(result.current.error).toBeTruthy();
    await act(() => result.current.enterEgo("person:a", "A"));
    expect(result.current.layer.kind).toBe("overview");
    expect(result.current.error).toBeTruthy();
  });
});
