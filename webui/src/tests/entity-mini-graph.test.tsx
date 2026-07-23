import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const api = {
  fetchMemorySubgraph: vi.fn(),
};
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchMemorySubgraph: (...a: unknown[]) => api.fetchMemorySubgraph(...a),
}));

import { EntityMiniGraph } from "@/components/EntityMiniGraph";

// The ego-subgraph around "person:x": the focus node itself (excluded from
// the ring), two real neighbours to rank by weight, and one session + one
// phantom neighbour that must never appear on the ring.
const SUBGRAPH = {
  nodes: [
    { id: "person:x", type: "person", name: "X", aliases: [], weight: 20 },
    { id: "person:bob", type: "person", name: "Bob", aliases: [], weight: 15 },
    { id: "project:acme", type: "project", name: "Acme", aliases: [], weight: 10 },
    { id: "session:abc123", type: "session", name: "abc123", aliases: [], weight: 1 },
    { id: "topic:ghost", type: "topic", name: "Ghost", aliases: [], weight: 1, phantom: true },
  ],
  edges: [],
  stats: {
    node_count: 5,
    edge_count: 0,
    phantom_count: 1,
    truncated_nodes: false,
    truncated_edges: false,
    types: ["person", "project", "topic"],
  },
};

const FOCUS_ONLY = {
  nodes: [{ id: "person:x", type: "person", name: "X", aliases: [], weight: 20 }],
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

function setup(overrides: Partial<React.ComponentProps<typeof EntityMiniGraph>> = {}) {
  const props = {
    token: "tok",
    entityRef: "person:x",
    entityName: "X",
    onNavigate: vi.fn(),
    onViewInGraph: vi.fn(),
    ...overrides,
  };
  const result = render(<EntityMiniGraph {...props} />);
  return { ...result, props };
}

describe("EntityMiniGraph", () => {
  it("renders 1-hop neighbours ranked by weight, excluding sessions and phantoms", async () => {
    api.fetchMemorySubgraph.mockReset().mockResolvedValue(SUBGRAPH);
    setup();

    expect(await screen.findByText("Bob")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.queryByText("abc123")).toBeNull();
    expect(screen.queryByText("Ghost")).toBeNull();
    expect(api.fetchMemorySubgraph).toHaveBeenCalledWith("tok", "person:x", { hops: 1 });
  });

  it("clicking a neighbour calls onNavigate with its ref and name", async () => {
    api.fetchMemorySubgraph.mockReset().mockResolvedValue(SUBGRAPH);
    const { props } = setup();
    const user = userEvent.setup();

    const bob = await screen.findByText("Bob");
    await user.click(bob);

    expect(props.onNavigate).toHaveBeenCalledWith("person:bob", "Bob");
  });

  it("the view-in-graph button calls onViewInGraph", async () => {
    api.fetchMemorySubgraph.mockReset().mockResolvedValue(SUBGRAPH);
    const { props } = setup();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /view in graph/i }));

    expect(props.onViewInGraph).toHaveBeenCalledTimes(1);
  });

  it("shows an empty state when there are no neighbours", async () => {
    api.fetchMemorySubgraph.mockReset().mockResolvedValue(FOCUS_ONLY);
    setup();

    expect(await screen.findByText("No relations yet.")).toBeInTheDocument();
  });

  it("hides silently on a fetch error", async () => {
    api.fetchMemorySubgraph.mockReset().mockRejectedValue(new Error("boom"));
    const { container } = setup();

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
