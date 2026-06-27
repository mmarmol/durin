import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { WorkItem } from "@/lib/types";
import { WorkPanel } from "./WorkPanel";

// i18n is initialized globally in src/tests/setup.ts — no wrapper needed.

const workflowItem: WorkItem = {
  kind: "workflow",
  id: "r1",
  label: "research-to-answer",
  status: "running",
  nodes: [],
  startedAt: 0,
  endedAt: null,
};

const finishedItem: WorkItem = {
  kind: "workflow",
  id: "r2",
  label: "done-workflow",
  status: "done",
  nodes: [],
  startedAt: 0,
  endedAt: 100,
};

describe("WorkPanel", () => {
  it("shows active work when open and hides when closed", () => {
    const { rerender } = render(
      <WorkPanel active={[workflowItem]} finished={[]} open onClose={() => {}} />,
    );
    expect(screen.getByText("research-to-answer")).toBeInTheDocument();
    rerender(
      <WorkPanel active={[workflowItem]} finished={[]} open={false} onClose={() => {}} />,
    );
    expect(screen.queryByText("research-to-answer")).not.toBeInTheDocument();
  });

  it("hides finished items by default and shows them after clicking the header", async () => {
    const user = userEvent.setup();
    render(
      <WorkPanel active={[]} finished={[finishedItem]} open onClose={() => {}} />,
    );
    // Collapsed by default — the item label is not rendered
    expect(screen.queryByText("done-workflow")).not.toBeInTheDocument();
    // Click the collapsible header to expand
    await user.click(screen.getByRole("button", { name: /finished/i }));
    expect(screen.getByText("done-workflow")).toBeInTheDocument();
  });

  it("shows an empty hint when no active items", () => {
    render(
      <WorkPanel active={[]} finished={[]} open onClose={() => {}} />,
    );
    // The empty hint key is work.empty
    expect(screen.getByText("No active work")).toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <WorkPanel active={[workflowItem]} finished={[]} open onClose={onClose} />,
    );
    await user.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
