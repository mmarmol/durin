import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DurinClient } from "@/lib/durin-client";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchMemoryGraph: vi.fn(),
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

const STATS = {
  node_count: 0,
  edge_count: 0,
  phantom_count: 0,
  truncated_nodes: false,
  truncated_edges: false,
  types: [] as string[],
};

const EMPTY_GRAPH = { nodes: [], edges: [], stats: STATS };

// Two consolidated entities — the normal case.
const RAW_DATA = {
  nodes: [
    { id: "person:aurora", type: "person", name: "Aurora", aliases: [], weight: 5 },
    { id: "person:borealis", type: "person", name: "Borealis", aliases: [], weight: 3 },
  ],
  edges: [],
  stats: { ...STATS, node_count: 2, types: ["person"] },
};

// Only scaffolding — a session node and nothing consultable. The
// inventory must treat this as an empty workspace, not as "nothing matches".
const SCAFFOLDING_ONLY = {
  nodes: [{ id: "session:s1", type: "session", name: "s1", aliases: [], weight: 9 }],
  edges: [],
  stats: { ...STATS, node_count: 1, types: ["session"] },
};

// A pre-dream workspace: the only entity is still a phantom, hidden by the
// default type filter. Not empty — filtered.
const PHANTOM_ONLY = {
  nodes: [
    { id: "topic:ghost", type: "topic", name: "Ghost", aliases: [], weight: 2, phantom: true },
  ],
  edges: [],
  stats: { ...STATS, node_count: 1, phantom_count: 1, types: ["topic"] },
};

const NEIGHBOURHOOD = {
  nodes: [{ id: "person:ada", type: "person", name: "Ada", aliases: [], weight: 12 }],
  edges: [],
  stats: { ...STATS, node_count: 1, types: ["person"] },
};

const ENTITY_DETAIL = {
  ref: "person:aurora",
  page: {
    type: "person",
    name: "Aurora",
    aliases: [],
    identifiers: null,
    extra: {},
    body: "Aurora is a person.",
    dream_processed_through: null,
  },
  provenance: [],
  history: [],
  archive: [],
  entries: [],
};

/** Every status-like message the main pane can show. A test asserts on the
 *  count so two explanations can never be on screen at once. */
function paneMessages(): string[] {
  const texts = [
    "Loading…",
    "Nothing here yet",
    "No entities match your search.",
    "Every entity is hidden by the type filter.",
  ];
  return texts.filter((text) => screen.queryByText(text) != null);
}

describe("MemoryGraphView main-pane states", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(api.fetchMemoryGraph).mockReset().mockResolvedValue(EMPTY_GRAPH);
    vi.mocked(api.fetchMemorySubgraph).mockReset().mockResolvedValue(NEIGHBOURHOOD);
    vi.mocked(api.fetchMemoryEntity).mockReset().mockResolvedValue(null);
  });

  it("shows only the loading message while the first load is in flight", async () => {
    let resolve: (v: typeof RAW_DATA) => void = () => {};
    vi.mocked(api.fetchMemoryGraph).mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    render(wrap(<MemoryGraphView active />));

    expect(await screen.findByRole("status")).toHaveTextContent("Loading…");
    expect(paneMessages()).toEqual(["Loading…"]);
    expect(document.querySelector("table")).toBeNull();

    resolve(RAW_DATA);
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    expect(paneMessages()).toEqual([]);
  });

  it("shows one teaching empty state, and no filter message, for an empty workspace", async () => {
    render(wrap(<MemoryGraphView active />));

    expect(await screen.findByText("Nothing here yet")).toBeInTheDocument();
    expect(paneMessages()).toEqual(["Nothing here yet"]);
    expect(document.querySelector("table")).toBeNull();
  });

  it("treats a payload with only session scaffolding as an empty workspace", async () => {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(SCAFFOLDING_ONLY);
    render(wrap(<MemoryGraphView active />));

    expect(await screen.findByText("Nothing here yet")).toBeInTheDocument();
    expect(paneMessages()).toEqual(["Nothing here yet"]);
  });

  it("says the type filter is hiding everything when only phantoms exist", async () => {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(PHANTOM_ONLY);
    render(wrap(<MemoryGraphView active />));

    expect(
      await screen.findByText("Every entity is hidden by the type filter."),
    ).toBeInTheDocument();
    expect(paneMessages()).toEqual(["Every entity is hidden by the type filter."]);
  });

  it("says nothing matches the search, and only that, when the query excludes every entity", async () => {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    const user = userEvent.setup();
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("Search memory…"), "zzz");

    expect(await screen.findByText("No entities match your search.")).toBeInTheDocument();
    expect(paneMessages()).toEqual(["No entities match your search."]);
  });

  it("keeps the list on screen during a refresh instead of flashing the loading message", async () => {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue(RAW_DATA);
    const user = userEvent.setup();
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());

    vi.mocked(api.fetchMemoryGraph).mockReturnValue(new Promise(() => {}));
    await user.click(screen.getByRole("button", { name: "Refresh" }));

    expect(screen.getByText("Aurora")).toBeInTheDocument();
    expect(paneMessages()).toEqual([]);
  });

  it("shows the error and a retry, nothing else, when the load fails", async () => {
    vi.mocked(api.fetchMemoryGraph).mockRejectedValue(new Error("boom"));
    render(wrap(<MemoryGraphView active />));

    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(paneMessages()).toEqual([]);
  });
});

describe("MemoryGraphView presentations", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(api.fetchMemoryGraph).mockReset().mockResolvedValue(RAW_DATA);
    vi.mocked(api.fetchMemorySubgraph).mockReset().mockResolvedValue(NEIGHBOURHOOD);
    vi.mocked(api.fetchMemoryEntity).mockReset().mockResolvedValue(null);
  });

  it("defaults to the table when no view preference is stored", async () => {
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    expect(document.querySelector("table")).not.toBeNull();
    expect(document.querySelector("canvas")).toBeNull();
  });

  it("offers only Table and Cards in the view switcher", async () => {
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Table" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cards" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Graph" })).toBeNull();
  });

  it('falls back to the table for a stored "graph" preference from an older install', async () => {
    localStorage.setItem("durin.memoryGraph.view.v2", "graph");
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    expect(document.querySelector("table")).not.toBeNull();
  });

  it('honors a stored "cards" preference under either key', async () => {
    localStorage.setItem("durin.memoryGraph.view", "cards");
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());
    expect(document.querySelector("table")).toBeNull();
  });

  it("does not list session or reference types in the type filter", async () => {
    vi.mocked(api.fetchMemoryGraph).mockResolvedValue({
      ...RAW_DATA,
      nodes: [
        ...RAW_DATA.nodes,
        { id: "session:s1", type: "session", name: "s1", aliases: [], weight: 9 },
        { id: "reference:doc", type: "reference", name: "Doc", aliases: [], weight: 1 },
      ],
      stats: { ...RAW_DATA.stats, types: ["person", "reference", "session"] },
    });
    const user = userEvent.setup();
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /types/i }));

    expect(screen.getByRole("button", { name: /^person/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^session/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^reference/ })).toBeNull();
  });

  it("opens the detail panel with the Related ring from the table", async () => {
    vi.mocked(api.fetchMemoryEntity).mockResolvedValue(ENTITY_DETAIL);
    const user = userEvent.setup();
    render(wrap(<MemoryGraphView active />));
    await waitFor(() => expect(screen.getByText("Aurora")).toBeInTheDocument());

    await user.click(screen.getByText("Aurora"));
    await user.click(await screen.findByRole("button", { name: "Info" }));

    expect(await screen.findByText("Related")).toBeInTheDocument();
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /view in graph/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /isolate/i })).toBeNull();
  });
});
