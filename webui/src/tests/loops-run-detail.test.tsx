import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityView } from "@/components/loops/ActivityView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAllLoopRuns: vi.fn(),
    answerLoopRun: vi.fn(),
    fireLoop: vi.fn(),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  if (!navigator.clipboard) {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: () => Promise.resolve() },
      configurable: true,
    });
  }
  vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
});
afterEach(() => vi.restoreAllMocks());

function wrap(children: ReactNode) {
  return (
    <ClientProvider
      client={{} as unknown as import("@/lib/durin-client").DurinClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

const RUN_WITH_EVIDENCE: api.LoopRun = {
  run_id: "run-evidence",
  loop: "digest",
  status: "done",
  source: "cron",
  task: "produce the daily digest and send it to the team",
  ask: null,
  detail: null,
  goal_reached: true,
  started_at: 1_700_000_000,
  finished_at: 1_700_000_100,
  origin: {
    channel: "email",
    sender: "user@example.com",
    chat_id: "user@example.com",
    thread: "thread-1234567890",
    subject: "Weekly digest request",
  },
  checks: [
    { kind: "script", required: true, ref: "pytest tests/", passed: true, detail: "3 passed" },
    { kind: "assertion", required: false, ref: "digest mentions revenue", passed: false, detail: "" },
  ],
  workflow_run_id: "wf-run-999",
};

const RUN_ERROR: api.LoopRun = {
  run_id: "run-error",
  loop: "cleanup",
  status: "error",
  source: "manual",
  task: "attempt cleanup",
  ask: null,
  detail: "the workflow node 'gate' crashed",
  goal_reached: false,
  started_at: 1_700_000_200,
  finished_at: 1_700_000_210,
  origin: null,
  checks: null,
  workflow_run_id: null,
};

const RUN_CLEAN: api.LoopRun = {
  run_id: "run-clean",
  loop: "support",
  status: "done",
  source: "manual",
  task: "short task",
  ask: null,
  detail: null,
  goal_reached: true,
  started_at: 1_700_000_300,
  finished_at: 1_700_000_310,
  origin: null,
  checks: null,
  workflow_run_id: null,
};

const RUN_NEEDS_OPERATOR: api.LoopRun = {
  run_id: "run-needs-operator",
  loop: "digest",
  status: "needs_operator",
  source: "cron",
  task: "daily digest",
  ask: "Which environment should this run against?",
  detail: null,
  goal_reached: null,
  started_at: 1_700_000_400,
  finished_at: null,
  origin: null,
  checks: null,
  workflow_run_id: null,
};

describe("loops RunDetail (ActivityView drill-in)", () => {
  it("expanding a run shows origin, checks, and workflow run evidence", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([RUN_WITH_EVIDENCE]);
    const user = userEvent.setup();
    render(wrap(<ActivityView />));

    await screen.findByText("digest");
    await user.click(screen.getByText("digest"));

    // Origin: channel, sender, subject, thread digest truncated to 8 chars.
    expect(screen.getByText("email")).toBeInTheDocument();
    expect(screen.getByText("user@example.com")).toBeInTheDocument();
    expect(screen.getByText("Weekly digest request")).toBeInTheDocument();
    const threadEl = screen.getByText("thread-1");
    expect(threadEl).toHaveAttribute("title", "thread-1234567890");

    // Checks table: kind, ref, passed/failed marks, detail text.
    expect(screen.getByText("script")).toBeInTheDocument();
    expect(screen.getByText("pytest tests/")).toBeInTheDocument();
    expect(screen.getByText("✓")).toBeInTheDocument();
    expect(screen.getByText("✗")).toBeInTheDocument();
    expect(screen.getByText("3 passed")).toBeInTheDocument();

    // Workflow run reference: copyable text (no in-app deep link into a
    // specific run exists in RunsView — it only tracks the open run via its
    // own local state, not a route or prop another view can set).
    expect(screen.getByText("wf-run-999")).toBeInTheDocument();
  });

  it("an error run shows the detail text in destructive tone", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([RUN_ERROR]);
    const user = userEvent.setup();
    render(wrap(<ActivityView />));

    await screen.findByText("cleanup");
    await user.click(screen.getByText("cleanup"));

    const detailEl = await screen.findByText("the workflow node 'gate' crashed");
    expect(detailEl).toHaveClass("text-destructive");
  });

  it("a run without checks or origin expands cleanly with no crash", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([RUN_CLEAN]);
    const user = userEvent.setup();
    render(wrap(<ActivityView />));

    await screen.findByText("support");
    await user.click(screen.getByText("support"));

    // The panel expands (its Task section label is unique to RunDetail —
    // the row itself uses the task text as its label, not this heading) and
    // the task text is shown both in the row and again in the detail panel.
    expect(await screen.findByText("Task")).toBeInTheDocument();
    expect(screen.getAllByText("short task")).toHaveLength(2);
    // ...but there is no checks section and no workflow run reference.
    expect(screen.queryByText("Checks")).not.toBeInTheDocument();
    expect(screen.queryByText("Workflow run")).not.toBeInTheDocument();
  });

  it("clicking into the answer input does not toggle the detail panel", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([RUN_NEEDS_OPERATOR]);
    const user = userEvent.setup();
    render(wrap(<ActivityView />));

    const input = await screen.findByPlaceholderText(/answer/i);
    await user.click(input);
    // Clicking the input must not expand the detail panel underneath.
    expect(screen.queryByText("Task")).not.toBeInTheDocument();

    // Clicking the row body elsewhere DOES expand it.
    await user.click(screen.getByText(RUN_NEEDS_OPERATOR.ask!));
    expect(await screen.findByText("Task")).toBeInTheDocument();
  });
});
