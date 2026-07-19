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

it("shows the stopping status word for a single stopping item", () => {
  const stopping = item({ status: "stopping" });
  render(wrap(<WorkStrip active={[stopping]} finished={[]} onOpen={() => {}} />));
  expect(screen.getByText("ticket-stage1-context")).toBeInTheDocument();
  expect(screen.getByText(/stopping/)).toBeInTheDocument();
  expect(screen.queryByText(/in progress/)).not.toBeInTheDocument();
});

it("flashes cancelled with the cancelled status word", () => {
  const { rerender } = render(
    wrap(<WorkStrip active={[item()]} finished={[]} onOpen={() => {}} />),
  );
  const cancelled = item({ status: "cancelled", endedAt: 2 });
  rerender(
    wrap(<WorkStrip active={[]} finished={[cancelled]} onOpen={() => {}} />),
  );
  expect(screen.getByText(/cancelled/)).toBeInTheDocument();
});
