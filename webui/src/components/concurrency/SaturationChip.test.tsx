import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useConcurrencySnapshot } from "@/hooks/useConcurrencySnapshot";
import { SaturationChip } from "./SaturationChip";

vi.mock("@/hooks/useConcurrencySnapshot", () => ({
  useConcurrencySnapshot: vi.fn(),
}));

const mockedSnapshot = vi.mocked(useConcurrencySnapshot);

function snapshot(active: number, limit: number, queued: number) {
  return {
    lanes: {
      interactive: { active: 0, limit: 4, waiting: 0 },
      ceiling: { active, limit, waiting: 0 },
      subagents: { active: 0, limit: 3 },
    },
    queued,
    work: [],
  };
}

describe("SaturationChip", () => {
  it("renders nothing until the first snapshot arrives", () => {
    mockedSnapshot.mockReturnValue(null);
    const { container } = render(<SaturationChip onOpen={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows ceiling occupancy with no amber styling when nothing is queued", () => {
    mockedSnapshot.mockReturnValue(snapshot(5, 12, 0));
    render(<SaturationChip onOpen={vi.fn()} />);
    expect(screen.getByText("5 / 12")).toBeInTheDocument();
    const button = screen.getByRole("button");
    expect(button.className).not.toMatch(/amber/);
  });

  it("shows amber styling and the queued count in the title when work is queued", () => {
    mockedSnapshot.mockReturnValue(snapshot(5, 12, 2));
    render(<SaturationChip onOpen={vi.fn()} />);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/amber/);
    expect(button.getAttribute("title")).toContain("2 queued");
  });

  it("calls onOpen when clicked", () => {
    mockedSnapshot.mockReturnValue(snapshot(5, 12, 0));
    const onOpen = vi.fn();
    render(<SaturationChip onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
