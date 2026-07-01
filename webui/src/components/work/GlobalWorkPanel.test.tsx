import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useConcurrencySnapshot } from "@/hooks/useConcurrencySnapshot";
import { GlobalWorkPanel } from "./GlobalWorkPanel";

vi.mock("@/hooks/useConcurrencySnapshot", () => ({
  useConcurrencySnapshot: vi.fn(),
}));

const mockedSnapshot = vi.mocked(useConcurrencySnapshot);

describe("GlobalWorkPanel", () => {
  it("renders nothing when closed", () => {
    mockedSnapshot.mockReturnValue({
      lanes: {
        interactive: { active: 0, limit: 4 },
        ceiling: { active: 0, limit: 12 },
        subagents: { active: 0, limit: 3 },
      },
      queued: 0,
      work: [],
    });
    const { container } = render(
      <GlobalWorkPanel open={false} onClose={vi.fn()} onOpenSettings={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the empty state when open with no running work", () => {
    mockedSnapshot.mockReturnValue({
      lanes: {
        interactive: { active: 0, limit: 4 },
        ceiling: { active: 0, limit: 12 },
        subagents: { active: 0, limit: 3 },
      },
      queued: 0,
      work: [],
    });
    render(<GlobalWorkPanel open onClose={vi.fn()} onOpenSettings={vi.fn()} />);
    expect(screen.getByText("Nothing running")).toBeInTheDocument();
  });

  it("renders one card per work item, labeled by kind fallback", () => {
    mockedSnapshot.mockReturnValue({
      lanes: {
        interactive: { active: 1, limit: 4 },
        ceiling: { active: 2, limit: 12 },
        subagents: { active: 1, limit: 3 },
      },
      queued: 0,
      work: [
        { kind: "turn", id: "turn-1", session_key: "websocket:chat-a", label: "", status: "running" },
        { kind: "subagent", id: "sub-1", session_key: "websocket:chat-a", label: "researcher", status: "running" },
      ],
    });
    render(<GlobalWorkPanel open onClose={vi.fn()} onOpenSettings={vi.fn()} />);
    expect(screen.getByText("Turn")).toBeInTheDocument();
    expect(screen.getByText("researcher")).toBeInTheDocument();
  });

  it("calls onOpenSettings when the gear button is clicked", () => {
    mockedSnapshot.mockReturnValue({
      lanes: {
        interactive: { active: 0, limit: 4 },
        ceiling: { active: 0, limit: 12 },
        subagents: { active: 0, limit: 3 },
      },
      queued: 0,
      work: [],
    });
    const onOpenSettings = vi.fn();
    render(<GlobalWorkPanel open onClose={vi.fn()} onOpenSettings={onOpenSettings} />);
    fireEvent.click(screen.getByRole("button", { name: "Concurrency" }));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when the close button is clicked", () => {
    mockedSnapshot.mockReturnValue({
      lanes: {
        interactive: { active: 0, limit: 4 },
        ceiling: { active: 0, limit: 12 },
        subagents: { active: 0, limit: 3 },
      },
      queued: 0,
      work: [],
    });
    const onClose = vi.fn();
    render(<GlobalWorkPanel open onClose={onClose} onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
