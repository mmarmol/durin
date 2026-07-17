import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import type { MemoryGraphNode } from "@/lib/api";
import { browseEntities } from "@/lib/memory-graph-style";
import { MemoryEntityCards } from "@/components/MemoryEntityCards";
import { MemoryEntityTable } from "@/components/MemoryEntityTable";

function node(over: Partial<MemoryGraphNode> & { id: string }): MemoryGraphNode {
  return {
    type: over.id.split(":")[0],
    name: over.id.split(":")[1],
    aliases: [],
    weight: 0,
    ...over,
  } as MemoryGraphNode;
}

const NODES: MemoryGraphNode[] = [
  node({
    id: "practice:laparoscopy",
    name: "Laparoscopy",
    weight: 14,
    summary: "Minimally invasive surgery evaluated for cysts.",
    updated_at: "2026-07-01T12:00:00+00:00",
    sources: 2,
  }),
  node({
    id: "person:marcelo",
    name: "Marcelo",
    aliases: ["mmarmol"],
    weight: 42,
    summary: "Architect and founder.",
    updated_at: "2026-07-15T09:00:00+00:00",
    sources: 6,
  }),
  node({
    id: "topic:creatinine",
    name: "creatinine",
    weight: 3,
    phantom: true,
    summary: null,
    updated_at: null,
    sources: 0,
  }),
  node({ id: "session:abc", name: "Some chat", weight: 9 }),
  node({ id: "reference:paper", name: "A paper", weight: 1 }),
];

const baseOpts = {
  hiddenTypes: new Set<string>(),
  query: "",
  sortKey: "recent" as const,
};

describe("browseEntities", () => {
  it("excludes sessions and references", () => {
    const out = browseEntities(NODES, baseOpts);
    expect(out.map((n) => n.id)).not.toContain("session:abc");
    expect(out.map((n) => n.id)).not.toContain("reference:paper");
    expect(out).toHaveLength(3);
  });

  it("respects hidden types including the phantom pseudo-type", () => {
    const out = browseEntities(NODES, {
      ...baseOpts,
      hiddenTypes: new Set(["person", "phantom"]),
    });
    expect(out.map((n) => n.id)).toEqual(["practice:laparoscopy"]);
  });

  it("filters by name, alias, and summary substring", () => {
    expect(
      browseEntities(NODES, { ...baseOpts, query: "mmarmol" }).map((n) => n.id),
    ).toEqual(["person:marcelo"]);
    expect(
      browseEntities(NODES, { ...baseOpts, query: "invasive" }).map((n) => n.id),
    ).toEqual(["practice:laparoscopy"]);
  });

  it("sorts by recency with timestamp-less nodes last", () => {
    const out = browseEntities(NODES, baseOpts);
    expect(out.map((n) => n.id)).toEqual([
      "person:marcelo",
      "practice:laparoscopy",
      "topic:creatinine",
    ]);
  });

  it("sorts by mentions", () => {
    const out = browseEntities(NODES, { ...baseOpts, sortKey: "mentions" });
    expect(out[0].id).toBe("person:marcelo");
    expect(out[1].id).toBe("practice:laparoscopy");
  });
});

describe("MemoryEntityCards", () => {
  it("renders a card per entity with summary and counts", () => {
    render(
      <MemoryEntityCards
        nodes={NODES}
        hiddenTypes={new Set()}
        query=""
        sortKey="recent"
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("Laparoscopy")).toBeInTheDocument();
    expect(
      screen.getByText("Minimally invasive surgery evaluated for cysts."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Some chat")).not.toBeInTheDocument();
  });

  it("marks phantoms and fires onSelect on click", () => {
    const onSelect = vi.fn();
    render(
      <MemoryEntityCards
        nodes={NODES}
        hiddenTypes={new Set()}
        query=""
        sortKey="recent"
        onSelect={onSelect}
      />,
    );
    expect(screen.getByText(/phantom/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Marcelo"));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "person:marcelo" }),
    );
  });

  it("shows the empty state when nothing matches", () => {
    render(
      <MemoryEntityCards
        nodes={NODES}
        hiddenTypes={new Set()}
        query="zzzz-no-match"
        sortKey="recent"
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText(/no entities match/i)).toBeInTheDocument();
  });
});

describe("MemoryEntityTable", () => {
  it("renders rows and sorts by the clicked column", () => {
    render(
      <MemoryEntityTable
        nodes={NODES}
        hiddenTypes={new Set()}
        query=""
        sortKey="recent"
        onSelect={() => {}}
      />,
    );
    const rowsBefore = screen
      .getAllByRole("row")
      .slice(1)
      .map((r) => r.textContent);
    expect(rowsBefore[0]).toContain("Marcelo");

    fireEvent.click(screen.getByRole("button", { name: "Entity" }));
    const rowsAfter = screen
      .getAllByRole("row")
      .slice(1)
      .map((r) => r.textContent);
    expect(rowsAfter[0]).toContain("creatinine");

    fireEvent.click(screen.getByRole("button", { name: "Entity" }));
    const rowsDesc = screen
      .getAllByRole("row")
      .slice(1)
      .map((r) => r.textContent);
    expect(rowsDesc[0]).toContain("Marcelo");
  });

  it("fires onSelect on row click", () => {
    const onSelect = vi.fn();
    render(
      <MemoryEntityTable
        nodes={NODES}
        hiddenTypes={new Set()}
        query=""
        sortKey="mentions"
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByText("Laparoscopy"));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "practice:laparoscopy" }),
    );
  });
});
