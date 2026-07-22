import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  fetchClusterSubgraph: vi.fn(),
  fetchMemorySubgraph: vi.fn(),
  fetchMemoryEntity: vi.fn(),
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

// Raw (uncapped) graph fixture behind fetchMemoryGraph — the payload Cards/
// Table read from. Two distinctly-named nodes so a test can tell "the full
// raw list" apart from "whatever a drill focused on".
const RAW_DATA = {
  nodes: [
    { id: "person:aurora", type: "person", name: "Aurora", aliases: [], weight: 5 },
    { id: "person:borealis", type: "person", name: "Borealis", aliases: [], weight: 3 },
  ],
  edges: [],
  stats: {
    node_count: 2,
    edge_count: 0,
    phantom_count: 0,
    truncated_nodes: false,
    truncated_edges: false,
    types: ["person"],
  },
};

// A drill's focus payload (ego neighbourhood): deliberately a single node
// that is NEITHER "Aurora" nor "Borealis", so if it leaks into a view that
// should still be showing the raw list, the raw names visibly go missing.
const EGO_FOCUS = {
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
};

describe("MemoryGraphView layered", () => {
  beforeEach(() => {
    // The view switcher persists to localStorage (see setViewPersisted) —
    // clear it so one test's view choice can't leak into the next test's
    // initial render.
    localStorage.clear();
    vi.mocked(api.fetchMemoryGraph).mockReset().mockResolvedValue(EMPTY_GRAPH);
    vi.mocked(api.fetchMemoryGraphOverview)
      .mockReset()
      .mockResolvedValue(CLUSTERED_OVERVIEW);
    vi.mocked(api.searchMemoryApi)
      .mockReset()
      .mockResolvedValue({ results: [], total: 0, strategy: "hybrid", ranking: "recency" });
    vi.mocked(api.fetchClusterSubgraph).mockReset().mockResolvedValue(null);
    vi.mocked(api.fetchMemorySubgraph).mockReset().mockResolvedValue(EGO_FOCUS);
    // Not asserted on directly in the tests below — mocked purely so
    // selecting a card doesn't fire a real network call (the panel header
    // that hosts the isolate button renders regardless of this resolving).
    vi.mocked(api.fetchMemoryEntity).mockReset().mockResolvedValue(null);
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

  // --- View-gating regressions -------------------------------------------
  // These guard the seam between the two-layer graph canvas (overview/
  // cluster/ego) and the Cards/Table presentations, which read the raw
  // entity list and must never see a canvas drill or the overview's totals.

  /** Mount with the clustered overview + RAW_DATA, then switch to Cards.
   *  Anchors on the clustered totals first (same anti-flake reasoning as
   *  the "hides the type filter" test above: an absence/presence check at
   *  t=0 would pass trivially, before either fetch resolves), then waits
   *  for a raw card to confirm Cards actually rendered before returning. */
  async function renderCardsView() {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    const user = userEvent.setup();
    render(wrap(<MemoryGraphView active />));
    await waitFor(() =>
      expect(screen.getByText(/1[.,]?238/)).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /cards/i }));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    return user;
  }

  /** From Cards view, select the "Aurora" card and click the panel's
   *  isolate/Focus control — the same canvas-drill machinery a canvas click
   *  would trigger (enterEgo), just entered from the panel instead. */
  async function selectAuroraAndIsolate(
    user: ReturnType<typeof userEvent.setup>,
  ) {
    await user.click(screen.getByText("Aurora"));
    const isolateBtn = await screen.findByRole("button", {
      name: /isolate neighbourhood/i,
    });
    await user.click(isolateBtn);
    // enterEgo's fetch resolves on a microtask; flush the resulting
    // setFocusGraph/setLayer updates before the caller asserts on them —
    // there's no DOM signal to `waitFor` on instead, since (post-fix) Cards
    // must render identically whether or not the drill has landed.
    await act(async () => {});
  }

  it("keeps Cards on the raw entity list after a canvas drill focuses a subgraph", async () => {
    const user = await renderCardsView();
    await selectAuroraAndIsolate(user);

    expect(vi.mocked(api.fetchMemorySubgraph)).toHaveBeenCalledWith(
      "tok",
      "person:aurora",
      { hops: 1 },
    );
    // The drill resolved to EGO_FOCUS (a single "Ada" node) — Cards must
    // still list both raw entities instead of falling back to the focus.
    // Selecting Aurora also opened the detail panel, which repeats its name
    // in a "font-semibold" title — scope to the card's own "font-medium"
    // name element to disambiguate from that panel duplicate.
    expect(
      screen.getByText("Aurora", { selector: ".font-medium" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Borealis")).toBeInTheDocument();
  });

  it("keeps the Cards header on the raw node/edge counts, not the overview's honest totals", async () => {
    await renderCardsView();

    expect(screen.queryByText(/1[.,]?238/)).toBeNull();
    // RAW_DATA is 2 nodes / 0 edges — the same nodesCount/edgesCount
    // rendering the "flat mode" test above already relies on.
    expect(screen.getByText(/2 nodes/)).toBeInTheDocument();
  });

  it("Esc closes the type filter popover without also exiting the graph drill underneath it", async () => {
    const user = await renderCardsView();
    await selectAuroraAndIsolate(user);

    // Breadcrumb (drill state) only renders in Graph view — switch back to
    // it, and confirm the drill survived the round trip through Cards. The
    // detail panel (still open from selectAuroraAndIsolate) also shows
    // "Aurora" as its title, in a "font-semibold" element — scope to the
    // breadcrumb's own "font-medium" name span to disambiguate.
    await user.click(screen.getByRole("button", { name: /^graph$/i }));
    await screen.findByText("Aurora", { selector: ".font-medium" });

    await user.click(screen.getByRole("button", { name: /types/i }));
    const typeSearchInput = screen.getByPlaceholderText("Search type…");

    fireEvent.keyDown(typeSearchInput, { key: "Escape" });

    expect(screen.queryByPlaceholderText("Search type…")).toBeNull();
    // The drill must still be active — the breadcrumb still shows it.
    expect(
      screen.getByText("Aurora", { selector: ".font-medium" }),
    ).toBeInTheDocument();
  });

  // --- States: empty, error-keeps-view, stale notice ----------------------

  it("shows a teaching empty state when memory is empty", async () => {
    vi.mocked(api.fetchMemoryGraphOverview).mockResolvedValue({
      mode: "flat",
      bubbles: [],
      hubs: [],
      loose: [],
      edges: [],
      stats: {
        entity_count: 0,
        reference_count: 0,
        bubble_count: 0,
        loose_count: 0,
        phantom_count: 0,
        session_count: 0,
      },
    });
    // fetchMemoryGraph defaults to EMPTY_GRAPH (see beforeEach) — both halves
    // of the empty-state condition (raw list AND overview) confirm zero.
    render(wrap(<MemoryGraphView active />));
    await waitFor(() =>
      expect(screen.getByText(/grows as durin remembers/i)).toBeInTheDocument(),
    );
  });

  it("keeps the last overview and shows an inline error on refresh failure", async () => {
    render(wrap(<MemoryGraphView active />));
    await waitFor(() =>
      expect(screen.getByText(/1[.,]?238/)).toBeInTheDocument(),
    );
    vi.mocked(api.fetchMemoryGraphOverview).mockRejectedValue(new Error("boom"));
    // The header's refresh button also refreshes the two-layer overview
    // while in graph view (see MemoryGraphView) — this is the only control
    // that can reach a failing fetchMemoryGraphOverview from the DOM.
    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument(),
    );
    // Never blanked: the last good overview (and its honest total) survives
    // a refresh failure — only a slim banner announces the error.
    expect(screen.getByText(/1[.,]?238/)).toBeInTheDocument();
  });

  it("shows a dismissible stale-cluster pill after a drill 404s, and restores the overview", async () => {
    // A single bubble (no hubs/loose) so the click below is unambiguous —
    // see the dispatch comment for why this doesn't need real geometry.
    vi.mocked(api.fetchMemoryGraphOverview).mockResolvedValue({
      mode: "clustered",
      bubbles: [{ id: "topic:gone", name: "Gone", count: 5, types: ["topic"], top: [] }],
      hubs: [],
      loose: [],
      edges: [],
      stats: {
        entity_count: 5,
        reference_count: 0,
        bubble_count: 1,
        loose_count: 0,
        phantom_count: 0,
        session_count: 0,
      },
    });
    // fetchClusterSubgraph already defaults to null in beforeEach (the
    // stale/404 case) — kept implicit here since this test is exactly that
    // default path.
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText(/5 entities/)).toBeInTheDocument());

    // happy-dom's canvas never yields a 2D context, so MemoryGraphView's
    // RAF/auto-fit effect bails out before ever touching the camera (see
    // `if (!ctx) return;`) — the camera stays at its identity transform
    // {k:1, tx:0, ty:0} for the whole test. With wrapRef's clientWidth/
    // clientHeight both 0 under happy-dom and a single overview node, the
    // simNodes builder places that one node at exactly (cx, cy) = (40, 0).
    // A synthetic pointerdown+pointerup at that same screen point reproduces
    // the click-to-drill gesture deterministically, without depending on
    // real layout or the force simulation ever having ticked.
    const canvas = document.querySelector("canvas");
    if (!canvas) throw new Error("canvas not found");
    for (const type of ["pointerdown", "pointerup"]) {
      const evt = new Event(type, { bubbles: true, cancelable: true }) as PointerEvent & {
        clientX: number;
        clientY: number;
        pointerId: number;
      };
      Object.assign(evt, { clientX: 40, clientY: 0, pointerId: 1, button: 0 });
      canvas.dispatchEvent(evt);
    }

    await waitFor(() =>
      expect(screen.getByText(/changed since the map was built/i)).toBeInTheDocument(),
    );
    // The hook already fell back to the overview layer and refreshed it in
    // the background — the honest total is still there, not a blank canvas.
    expect(screen.getByText(/5 entities/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(screen.queryByText(/changed since the map was built/i)).toBeNull();
  });
});
