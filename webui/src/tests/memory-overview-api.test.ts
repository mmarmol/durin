import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchWithReauth = vi.fn();
vi.mock("@/lib/http", () => ({ fetchWithReauth: (...a: unknown[]) => fetchWithReauth(...a) }));

import { fetchClusterSubgraph, fetchMemoryGraphOverview } from "@/lib/api";

function ok(data: unknown) {
  return { ok: true, status: 200, json: async () => ({ data }) };
}

describe("memory overview api", () => {
  beforeEach(() => fetchWithReauth.mockReset());

  it("fetches the overview endpoint and unwraps data", async () => {
    fetchWithReauth.mockResolvedValue(ok({ mode: "flat", bubbles: [] }));
    const out = await fetchMemoryGraphOverview("tok");
    expect(fetchWithReauth.mock.calls[0][0]).toBe("/api/v1/memory/graph/overview");
    expect(out.mode).toBe("flat");
  });

  it("requests cluster scope with the ref and maps 404 to null", async () => {
    fetchWithReauth.mockResolvedValue({ ok: false, status: 404, json: async () => ({}) });
    const out = await fetchClusterSubgraph("tok", "topic:m0");
    const url = String(fetchWithReauth.mock.calls[0][0]);
    expect(url).toContain("/api/v1/memory/subgraph?");
    expect(url).toContain("ref=topic%3Am0");
    expect(url).toContain("scope=cluster");
    expect(out).toBeNull();
  });
});
