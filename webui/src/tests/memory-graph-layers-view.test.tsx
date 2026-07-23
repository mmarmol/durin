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
    // Table is the first-run default (no stored preference) as of Change 1 —
    // covered by its own test below, which clears this back out. Every other
    // test here exercises graph-view-specific behaviour (the clustered
    // overview, canvas drills, breadcrumb, ...), so seed the v2 preference
    // key directly, as if the user had already made a genuine post-migration
    // choice of Graph — seeding the legacy key instead would just be reset
    // back to table by the one-time migration (covered separately below).
    localStorage.setItem("durin.memoryGraph.view.v2", "graph");
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
    // Change 1: grouped rendering is no longer the default — opt into the
    // "structure" (community) dimension to exercise it.
    localStorage.setItem("durin.memoryGraph.graphMode", "structure");
    render(wrap(<MemoryGraphView active />));
    await waitFor(() =>
      expect(screen.getByText(/1[.,]?238/)).toBeInTheDocument(),
    );
  });

  it("hides the type filter in the clustered overview, even though hub types exist", async () => {
    localStorage.setItem("durin.memoryGraph.graphMode", "structure");
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
    localStorage.setItem("durin.memoryGraph.graphMode", "structure");
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
    // Change 1: the graph's default sub-mode is ungrouped, which would show
    // RAW_DATA's flat counts instead of the clustered totals this helper
    // anchors on below — opt into "structure" so that anchor still means
    // "the overview resolved" the way it did before grouping became a
    // choice.
    localStorage.setItem("durin.memoryGraph.graphMode", "structure");
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
    localStorage.setItem("durin.memoryGraph.graphMode", "structure");
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

  it("shows the overview error banner without blanking the still-loaded raw graph", async () => {
    // The reachable middle state: the raw graph loaded fine (fetchMemoryGraph
    // resolves) but the overview never has — layers.overview stays null while
    // layers.error is set. Pre-fix, neither the slim banner (gated on a
    // last-good overview) nor the full-frame error (gated on rawData == null)
    // fires, so the failure is completely silent.
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    vi.mocked(api.fetchMemoryGraphOverview).mockRejectedValue(new Error("boom"));
    render(wrap(<MemoryGraphView active />));

    await waitFor(() =>
      expect(screen.getByText(/refresh the graph/i)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // Exactly one error surface (the slim banner) — not also the full-frame
    // duplicate, which would mean the raw view got replaced instead of kept.
    expect(screen.queryAllByText(/refresh the graph/i)).toHaveLength(1);
    // The canvas is still the thing on screen, driven by the raw graph.
    expect(document.querySelector("canvas")).not.toBeNull();
  });

  it("shows a dismissible stale-cluster pill after a drill 404s, and restores the overview", async () => {
    // This test's click-to-drill gesture targets a bubble, which only draws
    // in a grouped sub-mode — opt into "structure" (see Change 1).
    localStorage.setItem("durin.memoryGraph.graphMode", "structure");
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

  // --- Change 1/2: list-first default, group-by selector ------------------

  it("defaults to the table/list view when no view preference is stored", async () => {
    // Override the shared beforeEach's seeded v2 preference — this is the
    // one test in the file that must see the true first-run default (no
    // keys at all, so the migration seeds v2 with "table").
    localStorage.clear();
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    // No canvas ever mounts for the table presentation.
    expect(document.querySelector("canvas")).toBeNull();
  });

  it('resets a legacy "graph" preference to table instead of honoring it', async () => {
    // Pre-list-first-redesign installs stored "graph" under the legacy key
    // as the OLD DEFAULT, not a deliberate choice — on disk it is
    // indistinguishable from one, so the one-time migration resets it to
    // the new list-first default rather than reopening these users on the
    // graph forever.
    localStorage.clear();
    localStorage.setItem("durin.memoryGraph.view", "graph");
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    expect(document.querySelector("canvas")).toBeNull();
  });

  it('migrates a legacy "cards" preference to v2 and shows cards', async () => {
    // Unlike legacy "graph", a legacy "table"/"cards" value really was a
    // deliberate choice — it carries over to v2 verbatim instead of
    // resetting.
    localStorage.clear();
    localStorage.setItem("durin.memoryGraph.view", "cards");
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    expect(document.querySelector("canvas")).toBeNull();
    expect(document.querySelector("table")).toBeNull();
    expect(localStorage.getItem("durin.memoryGraph.view.v2")).toBe("cards");
  });

  it('honors a v2 "graph" preference as a genuine post-migration choice', async () => {
    localStorage.clear();
    localStorage.setItem("durin.memoryGraph.view.v2", "graph");
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(document.querySelector("canvas")).not.toBeNull());
  });

  it("defaults the graph to ungrouped — flat header counts, not the overview's totals", async () => {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    render(wrap(<MemoryGraphView active />));

    // RAW_DATA is 2 nodes / 0 edges — the flat counts show even though a
    // clustered overview (CLUSTERED_OVERVIEW, 1,238 entities) is available
    // and gets fetched in the background (hubs/stats reuse).
    await waitFor(() => expect(screen.getByText(/2 nodes/)).toBeInTheDocument());
    expect(screen.queryByText(/1[.,]?238/)).toBeNull();

    expect(screen.getByRole("button", { name: "Ungrouped" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Structure" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: "Type" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("switches to Structure and shows the overview's honest totals", async () => {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    const user = userEvent.setup();
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText(/2 nodes/)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Structure" }));

    await waitFor(() =>
      expect(screen.getByText(/1[.,]?238/)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/2 nodes/)).toBeNull();
  });

  it('migrates a stored legacy "clusters" graphMode to "structure"', async () => {
    localStorage.setItem("durin.memoryGraph.graphMode", "clusters");
    render(wrap(<MemoryGraphView active />));

    await waitFor(() =>
      expect(screen.getByText(/1[.,]?238/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Structure" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("keeps the group-by selector visible and usable when a grouped mode's overview reports flat", async () => {
    // "Structure" is picked, but this dimension's overview comes back flat
    // (too small/homogeneous to bubble) — overviewGraph is null even though
    // graphMode isn't "all". Pre-fix, showGraphModeToggle required
    // overviewGraph != null, so the selector vanished with no in-app way
    // back to Ungrouped.
    localStorage.setItem("durin.memoryGraph.graphMode", "structure");
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    vi.mocked(api.fetchMemoryGraphOverview).mockResolvedValue({
      ...CLUSTERED_OVERVIEW,
      mode: "flat",
    });
    const user = userEvent.setup();
    render(wrap(<MemoryGraphView active />));
    // The seam already falls back to the raw graph when the overview is
    // flat — RAW_DATA's flat counts confirm content actually rendered.
    await waitFor(() => expect(screen.getByText(/2 nodes/)).toBeInTheDocument());

    const structureBtn = screen.getByRole("button", { name: "Structure" });
    expect(structureBtn).toBeInTheDocument();
    expect(structureBtn).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "Ungrouped" }));
    expect(screen.getByRole("button", { name: "Ungrouped" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("orders the view switcher Table, Cards, Graph", async () => {
    render(wrap(<MemoryGraphView active />));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^table$/i })).toBeInTheDocument(),
    );
    const buttons = screen.getAllByRole("button", {
      name: /^(table|cards|graph)$/i,
    });
    expect(buttons.map((b) => b.textContent?.trim())).toEqual([
      "Table",
      "Cards",
      "Graph",
    ]);
  });

  // --- Change 2: disconnected pseudo-filter -------------------------------

  it('shows a "no connections" pseudo-row in the type filter, hidden by default, but never in Cards view', async () => {
    // 2 connected (an edge between them) + 3 isolated nodes.
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue({
      nodes: [
        { id: "person:aurora", type: "person", name: "Aurora", aliases: [], weight: 5 },
        { id: "person:borealis", type: "person", name: "Borealis", aliases: [], weight: 3 },
        { id: "topic:lonely1", type: "topic", name: "Lonely1", aliases: [], weight: 1 },
        { id: "topic:lonely2", type: "topic", name: "Lonely2", aliases: [], weight: 1 },
        { id: "topic:lonely3", type: "topic", name: "Lonely3", aliases: [], weight: 1 },
      ],
      edges: [{ source: "person:aurora", target: "person:borealis", weight: 2 }],
      stats: {
        node_count: 5,
        edge_count: 1,
        phantom_count: 0,
        truncated_nodes: false,
        truncated_edges: false,
        types: ["person", "topic"],
      },
    });
    const user = userEvent.setup();
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText(/5 nodes/)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /types/i }));
    const row = screen.getByText("no connections").closest("button");
    expect(row).not.toBeNull();
    expect(row).toHaveTextContent("3");
    // Default-hidden, matching the phantom pseudo-type's default.
    expect(row).toHaveAttribute("aria-pressed", "false");

    await user.click(row!);
    expect(row).toHaveAttribute("aria-pressed", "true");

    // Cards has no connectivity awareness at all — disconnectedIds is a
    // raw-graph-canvas concept (gated on renderingRawGraph). The pseudo-row
    // must not follow the shared toolbar into Cards, where toggling it would
    // visibly do nothing.
    await user.click(screen.getByRole("button", { name: /cards/i }));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /types/i }));
    expect(screen.queryByText("no connections")).toBeNull();
  });

  // --- Change 3: Related mini-graph panel integration ---------------------

  const ENTITY_DETAIL = {
    ref: "person:aurora",
    page: {
      type: "person",
      name: "Aurora",
      aliases: [],
      identifiers: null,
      extra: {},
      body: "",
      dream_processed_through: null,
    },
    provenance: [],
    history: [],
    archive: [],
    entries: [],
  };

  it("renders the Related mini-graph section when opening a real entity's panel from Cards", async () => {
    vi.mocked(api.fetchMemoryEntity).mockResolvedValue(ENTITY_DETAIL);
    const user = await renderCardsView();
    await user.click(screen.getByText("Aurora"));
    // Selecting a non-phantom entity opens on the Body tab by default — the
    // mini-graph lives in Info (see MemoryGraphView), so switch there first.
    await user.click(await screen.findByRole("button", { name: "Info" }));

    expect(await screen.findByText("Related")).toBeInTheDocument();
    // EGO_FOCUS (the shared mocked fetchMemorySubgraph result) has one
    // neighbour, Ada — its presence confirms the mini-graph actually
    // fetched and rendered, not just the section header.
    expect(await screen.findByText("Ada")).toBeInTheDocument();
  });
});
