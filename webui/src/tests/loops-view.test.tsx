import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoopsView } from "@/components/LoopsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listLoops: vi.fn(),
    listAllLoopRuns: vi.fn(),
    answerLoopRun: vi.fn(),
    deleteLoop: vi.fn(),
    fireLoop: vi.fn(),
    getLoopStats: vi.fn(),
  };
});

// Both LoopsView panes are always mounted (only hidden via CSS), so every
// test renders both ActivityView and DefinitionsView regardless of which tab
// it cares about — give both fetches a harmless default so a test that only
// cares about one side doesn't crash on the other's unmocked call.
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listAllLoopRuns).mockResolvedValue([]);
  vi.mocked(api.listLoops).mockResolvedValue([]);
  // Empty outcomes by default so the strip stays hidden in tests that don't
  // care about it (only the tests below assert on its content).
  vi.mocked(api.getLoopStats).mockResolvedValue({
    name: "digest",
    outcomes: [],
    convergence: null,
    escalation_rate: null,
    counts: {},
    pending_events: 0,
  });
  window.confirm = vi.fn();
  localStorage.removeItem("durin.loops.activityView");
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

const NEEDS_OPERATOR: api.LoopRun = {
  run_id: "run-waiting",
  loop: "digest",
  status: "needs_operator",
  source: "cron",
  task: "daily digest",
  ask: "Which environment should this run against?",
  detail: null,
  goal_reached: null,
  started_at: 1000,
  finished_at: null,
  origin: null,
  checks: null,
  workflow_run_id: null,
};

const ESCALATED: api.LoopRun = {
  run_id: "run-escalated",
  loop: "digest",
  status: "escalated",
  source: "cron",
  task: "daily digest",
  ask: null,
  detail: null,
  goal_reached: false,
  started_at: 3000,
  finished_at: 3100,
  origin: null,
  checks: null,
  workflow_run_id: null,
};

const DONE: api.LoopRun = {
  run_id: "run-done",
  loop: "cleanup",
  status: "done",
  source: "manual",
  task: "cleanup old files",
  ask: null,
  detail: null,
  goal_reached: true,
  started_at: 2000,
  finished_at: 2100,
  origin: null,
  checks: null,
  workflow_run_id: null,
};

const WAITING_INFO: api.LoopRun = {
  run_id: "run-waiting-info",
  loop: "support",
  status: "waiting_info",
  source: "channel",
  task: "help ticket",
  ask: "Can you share the account id?",
  detail: null,
  goal_reached: null,
  started_at: 4000,
  finished_at: null,
  origin: { channel: "email", sender: "user@example.com", chat_id: "user@example.com", thread: "t1", subject: "Help" },
  checks: null,
  workflow_run_id: null,
};

const RUNNING: api.LoopRun = {
  run_id: "run-running",
  loop: "sync",
  status: "running",
  source: "cron",
  task: "sync inventory",
  ask: null,
  detail: null,
  goal_reached: null,
  started_at: 5000,
  finished_at: null,
  origin: null,
  checks: null,
  workflow_run_id: null,
};

const LOOP_DEF: api.LoopSummary = {
  name: "digest",
  enabled: true,
  workflow: "digest-wf",
  goal: { intent: "send the digest", checks: [] },
  triggers: [{ source: "cron", schedule: { kind: "every", every_ms: 3600000 } }],
  concurrency: "single",
  stuck_after: 0,
  operator_channel: null,
  operator_to: null,
  active_runs: 1,
  needs_operator: 1,
  waiting_info: 0,
  pending_events: 0,
};

describe("LoopsView", () => {
  it("defaults to the activity tab and switches to definitions", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([DONE]);
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await screen.findByText("cleanup old files");
    const activityTab = screen.getByRole("button", { name: "Activity" });
    const definitionsTab = screen.getByRole("button", { name: "Definitions" });
    expect(activityTab).toHaveAttribute("aria-pressed", "true");
    expect(definitionsTab).toHaveAttribute("aria-pressed", "false");

    await user.click(definitionsTab);
    await screen.findByText("digest-wf");
    expect(definitionsTab).toHaveAttribute("aria-pressed", "true");
  });

  it("shows the ask text and an inline answer input for a needs_operator run", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([NEEDS_OPERATOR, DONE]);
    render(wrap(<LoopsView />));

    await screen.findByText(NEEDS_OPERATOR.ask!);
    expect(screen.getByPlaceholderText(/answer/i)).toBeInTheDocument();
  });

  it("answering a needs_operator run calls answerLoopRun with the typed text and refreshes", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([NEEDS_OPERATOR]);
    vi.mocked(api.answerLoopRun).mockResolvedValue({ ...NEEDS_OPERATOR, status: "running" });
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await screen.findByText(NEEDS_OPERATOR.ask!);
    const input = screen.getByPlaceholderText(/answer/i);
    await user.type(input, "staging");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() =>
      expect(api.answerLoopRun).toHaveBeenCalledWith("tok", "digest", "run-waiting", "staging"),
    );

    // After send, the input should be cleared (hidden) and sent message should appear
    await waitFor(() => expect(screen.getByText(/answer sent/i)).toBeInTheDocument());
    // The input should no longer be visible after sending
    expect(screen.queryByPlaceholderText(/answer/i)).not.toBeInTheDocument();

    await waitFor(() => expect(api.listAllLoopRuns).toHaveBeenCalledTimes(2));
  });

  it("a failed send keeps the typed answer and shows the error banner, no Answer sent", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([NEEDS_OPERATOR]);
    vi.mocked(api.answerLoopRun).mockRejectedValue(
      new api.ApiError(500, "HTTP 500", "boom"),
    );
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await screen.findByText(NEEDS_OPERATOR.ask!);
    const input = screen.getByPlaceholderText(/answer/i);
    await user.type(input, "staging");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await screen.findByText(/boom/i);
    expect(screen.queryByText(/answer sent/i)).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/answer/i)).toHaveValue("staging");
  });

  it("clicking Retry on an escalated run calls fireLoop and refreshes", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([ESCALATED]);
    vi.mocked(api.fireLoop).mockResolvedValue({ ...ESCALATED, status: "running" });
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await screen.findByText("daily digest");
    await user.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => expect(api.fireLoop).toHaveBeenCalledWith("tok", "digest"));
    await waitFor(() => expect(api.listAllLoopRuns).toHaveBeenCalledTimes(2));
  });

  it("a busy retry shows the error banner", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([ESCALATED]);
    vi.mocked(api.fireLoop).mockRejectedValue(
      new api.ApiError(422, "HTTP 422", "Loop 'digest' is busy: run-x still running"),
    );
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await screen.findByText("daily digest");
    await user.click(screen.getByRole("button", { name: /retry/i }));

    await screen.findByText(/is busy/i);
  });

  it("lists loop definitions in the definitions tab", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([]);
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");
    expect(screen.getByText("digest-wf")).toBeInTheDocument();
  });

  it("clicking Run now calls fireLoop with the loop name and refreshes", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    vi.mocked(api.fireLoop).mockResolvedValue({ ...DONE, loop: "digest" });
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");
    await user.click(screen.getByRole("button", { name: /Run now/i }));

    await waitFor(() => expect(api.fireLoop).toHaveBeenCalledWith("tok", "digest"));
    await waitFor(() => expect(api.listLoops).toHaveBeenCalledTimes(2));
  });

  it("a busy rejection from Run now shows the error banner, no crash", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    vi.mocked(api.fireLoop).mockRejectedValue(
      new api.ApiError(422, "HTTP 422", "Loop 'digest' is busy: run-x still running"),
    );
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");
    await user.click(screen.getByRole("button", { name: /Run now/i }));

    await screen.findByText(/is busy/i);
  });

  it("shows an empty state in both tabs when there is nothing to show", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([]);
    vi.mocked(api.listLoops).mockResolvedValue([]);
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await screen.findByText(/No loop runs/i);
    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText(/No loops yet/i);
  });

  it("deletes a loop definition via DeleteConfirm, never window.confirm", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    vi.mocked(api.deleteLoop).mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");
    await user.click(screen.getByRole("button", { name: /Delete/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(window.confirm).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/Delete loop digest\?/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /Delete/i }));
    await waitFor(() => expect(api.deleteLoop).toHaveBeenCalledWith("tok", "digest"));
  });

  it("shows the waiting_info badge and the ask read-only, with no answer input by default", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([WAITING_INFO]);
    render(wrap(<LoopsView />));

    await screen.findByText(WAITING_INFO.ask!);
    expect(screen.getByText(/waiting reply/i)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/answer/i)).not.toBeInTheDocument();
  });

  it("the operator-override toggle reveals the answer input and answering calls answerLoopRun", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([WAITING_INFO]);
    vi.mocked(api.answerLoopRun).mockResolvedValue({ ...WAITING_INFO, status: "running" });
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await screen.findByText(WAITING_INFO.ask!);
    expect(screen.queryByPlaceholderText(/answer/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /answer as operator/i }));
    const input = await screen.findByPlaceholderText(/answer/i);
    await user.type(input, "acct-123");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() =>
      expect(api.answerLoopRun).toHaveBeenCalledWith("tok", "support", "run-waiting-info", "acct-123"),
    );
  });

  it("shows a queued chip in Definitions when pending_events > 0", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([{ ...LOOP_DEF, pending_events: 3 }]);
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");
    expect(screen.getByText(/3 queued/i)).toBeInTheDocument();
  });

  it("the list/board toggle switches views and persists the choice in localStorage", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([DONE]);
    const user = userEvent.setup();
    const { unmount } = render(wrap(<LoopsView />));

    await screen.findByText("cleanup old files");
    const listBtn = screen.getByRole("button", { name: "List" });
    const boardBtn = screen.getByRole("button", { name: "Board" });
    expect(listBtn).toHaveAttribute("aria-pressed", "true");
    expect(boardBtn).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("group", { name: "done" })).not.toBeInTheDocument();

    await user.click(boardBtn);
    expect(await screen.findByRole("group", { name: "done" })).toBeInTheDocument();
    expect(boardBtn).toHaveAttribute("aria-pressed", "true");
    expect(localStorage.getItem("durin.loops.activityView")).toBe("board");

    // Re-render (simulating a reload) should read the persisted preference
    // and default straight into board mode.
    unmount();
    render(wrap(<LoopsView />));
    expect(await screen.findByRole("group", { name: "done" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Board" })).toHaveAttribute("aria-pressed", "true");
  });

  it("board view places each run under its correct column", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([
      NEEDS_OPERATOR,
      WAITING_INFO,
      RUNNING,
      DONE,
      ESCALATED,
    ]);
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(await screen.findByRole("button", { name: "Board" }));

    const needsGroup = await screen.findByRole("group", { name: "needs you" });
    const waitingGroup = screen.getByRole("group", { name: "waiting reply" });
    const runningGroup = screen.getByRole("group", { name: "running" });
    const doneGroup = screen.getByRole("group", { name: "done" });
    const attentionGroup = screen.getByRole("group", { name: "Attention" });

    expect(within(needsGroup).getByText("daily digest")).toBeInTheDocument();
    expect(within(waitingGroup).getByText("Help")).toBeInTheDocument();
    expect(within(runningGroup).getByText("sync inventory")).toBeInTheDocument();
    expect(within(doneGroup).getByText("cleanup old files")).toBeInTheDocument();
    expect(within(attentionGroup).getByText("daily digest")).toBeInTheDocument();

    // Cross-column placement should not bleed: the escalated run's card is
    // only under Attention, not under needs you.
    expect(within(needsGroup).queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    expect(within(attentionGroup).getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("answering from a board card calls answerLoopRun", async () => {
    vi.mocked(api.listAllLoopRuns).mockResolvedValue([NEEDS_OPERATOR]);
    vi.mocked(api.answerLoopRun).mockResolvedValue({ ...NEEDS_OPERATOR, status: "running" });
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(await screen.findByRole("button", { name: "Board" }));
    const input = await screen.findByPlaceholderText(/answer/i);
    await user.type(input, "staging");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() =>
      expect(api.answerLoopRun).toHaveBeenCalledWith("tok", "digest", "run-waiting", "staging"),
    );
  });

  it("renders the outcome strip oldest→newest with correct tone classes and percentages", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    // Newest-first from the route: r3 (escalated) is newest, r1 (done) oldest.
    vi.mocked(api.getLoopStats).mockResolvedValue({
      name: "digest",
      outcomes: [
        { run_id: "r3", status: "escalated", goal_reached: false, started_at: 3000, finished_at: 3100 },
        { run_id: "r2", status: "no_goal", goal_reached: false, started_at: 2000, finished_at: 2100 },
        { run_id: "r1", status: "done", goal_reached: true, started_at: 1000, finished_at: 1100 },
      ],
      convergence: 0.75,
      escalation_rate: 0.25,
      counts: {},
      pending_events: 0,
    });
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");

    const dots = await screen.findAllByTestId("outcome-dot");
    expect(dots).toHaveLength(3);
    // Oldest → newest left-to-right: done, no_goal, escalated.
    expect(dots[0]).toHaveClass("bg-primary");
    expect(dots[1]).toHaveClass("bg-muted-foreground/40");
    expect(dots[2]).toHaveClass("bg-destructive");

    expect(screen.getByText(/75%/)).toBeInTheDocument();
    expect(screen.getByText(/esc 25%/)).toBeInTheDocument();
  });

  it("hides the escalation percentage when the rate is zero", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    vi.mocked(api.getLoopStats).mockResolvedValue({
      name: "digest",
      outcomes: [
        { run_id: "r1", status: "done", goal_reached: true, started_at: 1000, finished_at: 1100 },
      ],
      convergence: 1,
      escalation_rate: 0,
      counts: {},
      pending_events: 0,
    });
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");

    await screen.findByText("100%");
    expect(screen.queryByText(/esc/)).not.toBeInTheDocument();
  });

  it("hides the outcome strip entirely when there are no outcomes", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    vi.mocked(api.getLoopStats).mockResolvedValue({
      name: "digest",
      outcomes: [],
      convergence: null,
      escalation_rate: null,
      counts: {},
      pending_events: 0,
    });
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");

    expect(screen.queryByTestId("outcome-dot")).not.toBeInTheDocument();
  });

  it("a failed stats fetch renders the row without a strip and without crashing", async () => {
    vi.mocked(api.listLoops).mockResolvedValue([LOOP_DEF]);
    vi.mocked(api.getLoopStats).mockRejectedValue(new api.ApiError(500, "HTTP 500"));
    const consoleDebug = vi.spyOn(console, "debug").mockImplementation(() => {});
    const user = userEvent.setup();
    render(wrap(<LoopsView />));

    await user.click(screen.getByRole("button", { name: /Definitions/i }));
    await screen.findByText("digest");

    await waitFor(() => expect(api.getLoopStats).toHaveBeenCalledWith("tok", "digest"));
    expect(screen.queryByTestId("outcome-dot")).not.toBeInTheDocument();
    expect(consoleDebug).toHaveBeenCalled();
    consoleDebug.mockRestore();
  });
});
