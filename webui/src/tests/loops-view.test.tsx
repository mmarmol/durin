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
  window.confirm = vi.fn();
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
  goal_reached: null,
  started_at: 1000,
  finished_at: null,
};

const ESCALATED: api.LoopRun = {
  run_id: "run-escalated",
  loop: "digest",
  status: "escalated",
  source: "cron",
  task: "daily digest",
  ask: null,
  goal_reached: false,
  started_at: 3000,
  finished_at: 3100,
};

const DONE: api.LoopRun = {
  run_id: "run-done",
  loop: "cleanup",
  status: "done",
  source: "manual",
  task: "cleanup old files",
  ask: null,
  goal_reached: true,
  started_at: 2000,
  finished_at: 2100,
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
});
