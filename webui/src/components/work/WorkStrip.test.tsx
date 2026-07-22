import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { WorkStrip } from "@/components/work/WorkStrip";
import type { WorkItem } from "@/lib/types";

function item(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    kind: "workflow",
    id: "r1",
    label: "ticket-stage1-context",
    status: "running",
    startedAt: 1,
    endedAt: null,
    ...overrides,
  };
}

function wrap(node: React.ReactNode) {
  return <I18nextProvider i18n={i18n}>{node}</I18nextProvider>;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

it("renders nothing when there is no active work", () => {
  const { container } = render(
    wrap(<WorkStrip active={[]} finished={[]} onOpen={() => {}} />),
  );
  expect(container).toBeEmptyDOMElement();
});

it("shows the label for a single running item and opens the panel on click", () => {
  const onOpen = vi.fn();
  render(wrap(<WorkStrip active={[item()]} finished={[]} onOpen={onOpen} />));
  expect(screen.getByText("ticket-stage1-context")).toBeInTheDocument();
  expect(screen.getByText(/in progress/)).toBeInTheDocument();
  expect(screen.getByText(/View/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button"));
  expect(onOpen).toHaveBeenCalledTimes(1);
});

it("shows a count for multiple running items", () => {
  const active = [item(), item({ id: "r2", label: "other-flow" })];
  render(wrap(<WorkStrip active={active} finished={[]} onOpen={() => {}} />));
  expect(screen.getByText("2 tasks in progress")).toBeInTheDocument();
});

it("prioritizes needs_input over running and offers Respond", () => {
  const active = [
    item(),
    item({ id: "r2", label: "deploy-approval", status: "needs_input" }),
  ];
  render(wrap(<WorkStrip active={active} finished={[]} onOpen={() => {}} />));
  expect(
    screen.getByText("deploy-approval needs your response"),
  ).toBeInTheDocument();
  expect(screen.getByText(/Respond/)).toBeInTheDocument();
  expect(screen.queryByText(/in progress/)).not.toBeInTheDocument();
});

it("does not drive the ticker for a single needs_input item whose blocking node is still status: running", () => {
  // WorkNode has no "paused" state, so the node a needs_input run is blocked
  // on most plausibly still reports status: "running" with a startedAt. The
  // warn branch never reads the node/clock, so this must not start the
  // 1-second ticker — it would otherwise spin for as long as the human takes
  // to answer.
  const waiting = item({
    status: "needs_input",
    nodes: [{ id: "gather", label: "Gather", status: "running", startedAt: 1 }],
  });
  render(wrap(<WorkStrip active={[waiting]} finished={[]} onOpen={() => {}} />));
  expect(
    screen.getByText("ticket-stage1-context needs your response"),
  ).toBeInTheDocument();
  expect(vi.getTimerCount()).toBe(0);
});

it("flashes finished when the last active item ends, then clears", () => {
  const { rerender, container } = render(
    wrap(<WorkStrip active={[item()]} finished={[]} onOpen={() => {}} />),
  );
  const done = item({ status: "done", endedAt: 2 });
  rerender(wrap(<WorkStrip active={[]} finished={[done]} onOpen={() => {}} />));
  expect(screen.getByText("ticket-stage1-context")).toBeInTheDocument();
  expect(screen.getByText(/finished/)).toBeInTheDocument();
  act(() => {
    vi.advanceTimersByTime(6500);
  });
  expect(container).toBeEmptyDOMElement();
});

it("flashes failed with the failed status word", () => {
  const { rerender } = render(
    wrap(<WorkStrip active={[item()]} finished={[]} onOpen={() => {}} />),
  );
  const failed = item({ status: "failed", endedAt: 2 });
  rerender(
    wrap(<WorkStrip active={[]} finished={[failed]} onOpen={() => {}} />),
  );
  expect(screen.getByText(/failed/)).toBeInTheDocument();
});

it("does not flash on first mount with empty active and old finished items", () => {
  const done = item({ status: "done", endedAt: 2 });
  const { container } = render(
    wrap(<WorkStrip active={[]} finished={[done]} onOpen={() => {}} />),
  );
  expect(container).toBeEmptyDOMElement();
});

it("shows the active node label and a ticking clock for a single running item", () => {
  vi.setSystemTime(new Date(2024, 0, 1, 0, 0, 0));
  const startedAt = Math.floor(Date.now() / 1000);
  const withNode = item({
    nodes: [
      { id: "gather", status: "done" },
      { id: "consolidate", label: "Consolidate", status: "running", startedAt },
    ],
  });
  render(wrap(<WorkStrip active={[withNode]} finished={[]} onOpen={() => {}} />));
  expect(screen.getByText("ticket-stage1-context")).toBeInTheDocument();
  expect(screen.getByText(/Consolidate · 0:00 · 2 nodes/)).toBeInTheDocument();
  expect(screen.queryByText(/in progress/)).not.toBeInTheDocument();

  act(() => {
    vi.advanceTimersByTime(5000);
  });
  expect(screen.getByText(/Consolidate · 0:05 · 2 nodes/)).toBeInTheDocument();
});

it("falls back to the status text when the item has nodes but none are running", () => {
  const withDoneNodes = item({ nodes: [{ id: "gather", status: "done" }] });
  render(wrap(<WorkStrip active={[withDoneNodes]} finished={[]} onOpen={() => {}} />));
  expect(screen.getByText(/in progress/)).toBeInTheDocument();
});

it("counts only the nodes the run has touched, excluding the pending tail", () => {
  vi.setSystemTime(new Date(2024, 0, 1, 0, 0, 0));
  const startedAt = Math.floor(Date.now() / 1000);
  const withPending = item({
    nodes: [
      { id: "gather", status: "done" },
      { id: "consolidate", label: "Consolidate", status: "running", startedAt },
      { id: "report", status: "pending" },
      { id: "notify", status: "pending" },
    ],
  });
  render(wrap(<WorkStrip active={[withPending]} finished={[]} onOpen={() => {}} />));
  // Two touched (done + running); the two-node pending tail is not counted.
  expect(screen.getByText(/Consolidate · 0:00 · 2 nodes/)).toBeInTheDocument();
  expect(screen.queryByText(/4 nodes/)).not.toBeInTheDocument();
});

it("uses the singular node count when only one node has been touched", () => {
  vi.setSystemTime(new Date(2024, 0, 1, 0, 0, 0));
  const startedAt = Math.floor(Date.now() / 1000);
  const withOneNode = item({
    nodes: [{ id: "gather", label: "Gather", status: "running", startedAt }],
  });
  render(wrap(<WorkStrip active={[withOneNode]} finished={[]} onOpen={() => {}} />));
  // Anchored on `$` so a regression to the plural ("1 nodes") would not
  // satisfy this match.
  expect(screen.getByText(/Gather · 0:00 · 1 node$/)).toBeInTheDocument();
});
