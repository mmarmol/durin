import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemoryTypeFilter, type TypeLegendItem } from "@/components/MemoryTypeFilter";

const TYPES: TypeLegendItem[] = [
  { type: "person", color: "#7C3AED", count: 7 },
  { type: "project", color: "#0EA5E9", count: 78 },
  { type: "topic", color: "#10B981", count: 115 },
];

function setup(overrides: Partial<React.ComponentProps<typeof MemoryTypeFilter>> = {}) {
  const props = {
    types: TYPES,
    phantomCount: 4,
    hidden: new Set<string>(),
    onToggle: vi.fn(),
    onShowAll: vi.fn(),
    onHideAll: vi.fn(),
    onSolo: vi.fn(),
    ...overrides,
  };
  render(<MemoryTypeFilter {...props} />);
  return props;
}

describe("MemoryTypeFilter", () => {
  it("shows the visible count and opens a searchable popover on click", async () => {
    const user = userEvent.setup();
    setup();
    const trigger = screen.getByRole("button", { name: /types/i });
    // 3 real types + phantom, none hidden = 4 visible
    expect(trigger).toHaveTextContent("4 visible");
    expect(screen.queryByPlaceholderText("Search type…")).toBeNull();

    await user.click(trigger);
    expect(screen.getByPlaceholderText("Search type…")).toBeInTheDocument();
    expect(screen.getByText("person")).toBeInTheDocument();
    expect(screen.getByText("115")).toBeInTheDocument(); // topic count
    expect(screen.getByText("phantom")).toBeInTheDocument(); // phantom pseudo-row
  });

  it("reflects hidden types in the visible count", () => {
    setup({ hidden: new Set(["person", "phantom"]) });
    // 2 real visible, phantom hidden = 2 visible
    expect(screen.getByRole("button", { name: /types/i })).toHaveTextContent("2 visible");
  });

  it("toggles a single type by clicking its row", async () => {
    const user = userEvent.setup();
    const props = setup();
    await user.click(screen.getByRole("button", { name: /types/i }));
    await user.click(screen.getByText("project"));
    expect(props.onToggle).toHaveBeenCalledWith("project");
  });

  it("filters the list by search", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByRole("button", { name: /types/i }));
    await user.type(screen.getByPlaceholderText("Search type…"), "top");
    expect(screen.getByText("topic")).toBeInTheDocument();
    expect(screen.queryByText("person")).toBeNull();
    expect(screen.queryByText("phantom")).toBeNull();
  });

  it("wires show all and hide all", async () => {
    const user = userEvent.setup();
    const props = setup();
    await user.click(screen.getByRole("button", { name: /types/i }));
    await user.click(screen.getByRole("button", { name: /hide all/i }));
    expect(props.onHideAll).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: /show all/i }));
    expect(props.onShowAll).toHaveBeenCalledTimes(1);
  });

  it("solos a type without also toggling it", async () => {
    const user = userEvent.setup();
    const props = setup();
    await user.click(screen.getByRole("button", { name: /types/i }));
    await user.click(screen.getByRole("button", { name: /only person/i }));
    expect(props.onSolo).toHaveBeenCalledWith("person");
    expect(props.onToggle).not.toHaveBeenCalled();
  });

  it("renders nothing when there are no types and no phantoms", () => {
    const { container } = render(
      <MemoryTypeFilter
        types={[]}
        phantomCount={0}
        hidden={new Set()}
        onToggle={vi.fn()}
        onShowAll={vi.fn()}
        onHideAll={vi.fn()}
        onSolo={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
