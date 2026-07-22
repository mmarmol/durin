import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DurinClient } from "@/lib/durin-client";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchMemoryGraph: vi.fn(),
  fetchMemoryGraphOverview: vi.fn(),
  searchMemoryApi: vi.fn(),
}));

import { MemoryGraphView } from "@/components/MemoryGraphView";

function wrap(children: ReactNode) {
  return (
    <ClientProvider client={{} as unknown as DurinClient} token="tok">
      {children}
    </ClientProvider>
  );
}

const EMPTY_GRAPH = {
  nodes: [],
  edges: [],
  stats: {
    node_count: 0,
    edge_count: 0,
    phantom_count: 0,
    truncated_nodes: false,
    truncated_edges: false,
    types: [],
  },
};

// Clustered overview fixture. Includes one hub with a real type so the
// "hides the type filter" test is meaningful — without a real-typed node,
// the type-filter popover would be empty (and thus absent) regardless of
// whether the clustered-overview gating is wired up at all.
const CLUSTERED_OVERVIEW = {
  mode: "clustered" as const,
  bubbles: [{ id: "topic:emailsync", name: "emailsync", count: 214, types: ["topic"], top: [] }],
  hubs: [{ id: "person:ada", type: "person", name: "Ada", aliases: [], weight: 12 }],
  loose: [],
  edges: [],
  stats: {
    entity_count: 1238,
    reference_count: 10,
    bubble_count: 9,
    loose_count: 4,
    phantom_count: 190,
    session_count: 2,
  },
};

describe("MemoryGraphView layered", () => {
  beforeEach(() => {
    vi.mocked(api.fetchMemoryGraph).mockReset().mockResolvedValue(EMPTY_GRAPH);
    vi.mocked(api.fetchMemoryGraphOverview)
      .mockReset()
      .mockResolvedValue(CLUSTERED_OVERVIEW);
    vi.mocked(api.searchMemoryApi)
      .mockReset()
      .mockResolvedValue({ results: [], total: 0, strategy: "hybrid", ranking: "recency" });
  });

  it("shows honest totals from the overview stats in clustered mode", async () => {
    render(wrap(<MemoryGraphView active />));
    await waitFor(() =>
      expect(screen.getByText(/1[.,]?238/)).toBeInTheDocument(),
    );
  });

  it("hides the type filter in the clustered overview, even though hub types exist", async () => {
    render(wrap(<MemoryGraphView active />));
    // Anchor on the clustered totals so the absence check happens after the
    // overview has actually loaded (an absence assertion checked at t=0 would
    // pass trivially, before either fetch resolves).
    await waitFor(() =>
      expect(screen.getByText(/1[.,]?238/)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /types/i })).toBeNull();
  });

  it("shows the type filter in flat mode", async () => {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue({
      nodes: [{ id: "person:ada", type: "person", name: "Ada", aliases: [], weight: 12 }],
      edges: [],
      stats: {
        node_count: 1,
        edge_count: 0,
        phantom_count: 0,
        truncated_nodes: false,
        truncated_edges: false,
        types: ["person"],
      },
    });
    vi.mocked(api.fetchMemoryGraphOverview).mockResolvedValue({
      ...CLUSTERED_OVERVIEW,
      mode: "flat",
    });
    render(wrap(<MemoryGraphView active />));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /types/i })).toBeInTheDocument(),
    );
  });
});
