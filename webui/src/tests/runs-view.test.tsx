import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import { RunNodeRow } from "@/components/workflows/RunDetail";
import { RunsView, strandedRuns } from "@/components/workflows/RunsView";
import * as api from "@/lib/api";
import { listAllWorkflowRuns } from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAllWorkflowRuns: vi.fn(),
    getWorkflowRunManifest: vi.fn(),
    runWorkflow: vi.fn(),
  };
});

beforeEach(() => {
  if (!navigator.clipboard) {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: () => Promise.resolve() },
      configurable: true,
    });
  }
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

const NEEDS_INPUT: api.WorkflowGlobalRun = {
  workflow: "onboarding",
  run_id: "run-waiting",
  status: "needs_input",
  started_at: 1000,
  finished_at: null,
  task: "set up the account",
  needs_input_node: "ask",
  questions: "Which environment — staging or prod?",
};

const COMPLETED: api.WorkflowGlobalRun = {
  workflow: "digest",
  run_id: "run-done",
  status: "completed",
  started_at: 2000,
  finished_at: 2100,
  task: "summarize the week",
  needs_input_node: null,
};

// A needs_input manifest written before the resume feature shipped: no
// needs_input_node (and thus no resume target), so it isn't an actionable
// stranded run even though its status is needs_input.
const LEGACY_NEEDS_INPUT: api.WorkflowGlobalRun = {
  workflow: "onboarding",
  run_id: "run-legacy",
  status: "needs_input",
  started_at: 500,
  finished_at: null,
  task: "old paused run",
  needs_input_node: null,
};

describe("strandedRuns", () => {
  it("filters to only needs_input entries", () => {
    expect(strandedRuns([NEEDS_INPUT, COMPLETED]).map((r) => r.run_id)).toEqual(["run-waiting"]);
  });

  it("is empty when nothing is waiting", () => {
    expect(strandedRuns([COMPLETED])).toEqual([]);
  });

  it("excludes needs_input entries with no needs_input_node (not resumable)", () => {
    expect(strandedRuns([NEEDS_INPUT, LEGACY_NEEDS_INPUT]).map((r) => r.run_id)).toEqual([
      "run-waiting",
    ]);
  });
});

describe("RunsView", () => {
  it("renders the tray with questions and posts a resume with {workflow, answers, run_id}", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, COMPLETED]);
    vi.mocked(api.runWorkflow).mockResolvedValue({
      status: "completed", final_output: "done", run_id: "run-waiting", runs: [],
    });
    const user = userEvent.setup();
    render(wrap(<RunsView />));

    await screen.findByText("set up the account");
    expect(screen.getByText(/Which environment — staging or prod\?/)).toBeInTheDocument();

    const textarea = screen.getByPlaceholderText(/Type your answers/i);
    await user.type(textarea, "prod");
    await user.click(screen.getByRole("button", { name: /Resume run/i }));

    await waitFor(() =>
      expect(api.runWorkflow).toHaveBeenCalledWith("tok", "onboarding", "prod", [], "", "", "run-waiting"),
    );
  });

  it("renders the feed with status chips for every run", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, COMPLETED]);
    render(wrap(<RunsView />));

    await screen.findByText("summarize the week");
    expect(screen.getByText("set up the account")).toBeInTheDocument();
  });

  it("filters the feed by status", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, COMPLETED]);
    const user = userEvent.setup();
    render(wrap(<RunsView />));

    await screen.findByText("summarize the week");
    await user.selectOptions(screen.getByLabelText(/Filter by status/i), "completed");
    expect(screen.getByText("summarize the week")).toBeInTheDocument();
    // The needs_input row's task text should no longer be shown once filtered out.
    expect(screen.queryByText("set up the account")).not.toBeInTheDocument();
  });

  it("filters the feed by workflow", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, COMPLETED]);
    const user = userEvent.setup();
    render(wrap(<RunsView />));

    await screen.findByText("summarize the week");
    await user.selectOptions(screen.getByLabelText(/Filter by workflow/i), "digest");
    expect(screen.getByText("summarize the week")).toBeInTheDocument();
    expect(screen.queryByText("set up the account")).not.toBeInTheDocument();
  });

  it("fetches and shows a run's manifest detail when a feed entry is clicked", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([COMPLETED]);
    vi.mocked(api.getWorkflowRunManifest).mockResolvedValue({
      status: "completed",
      final_output: "the weekly digest",
      run_id: "run-done",
      runs: [],
    });
    const user = userEvent.setup();
    render(wrap(<RunsView />));

    await screen.findByText("summarize the week");
    await user.click(screen.getByText("summarize the week"));

    expect(await screen.findByText("the weekly digest")).toBeInTheDocument();
    expect(api.getWorkflowRunManifest).toHaveBeenCalledWith("tok", "digest", "run-done");

    // The detail expands INLINE under the clicked row (same wrapper element),
    // not at the bottom of the whole feed — a long list must not hide it below
    // the fold where a click appears to do nothing.
    const row = screen.getByText("summarize the week").closest("div[class*='flex-col']");
    expect(row).not.toBeNull();
    expect(row!.textContent).toContain("the weekly digest");

    // Clicking the active row again collapses the detail (accordion toggle).
    await user.click(screen.getByText("summarize the week"));
    expect(screen.queryByText("the weekly digest")).not.toBeInTheDocument();
  });

  it("marks a sub-run whose parent is not in the list with a 'sub of' marker", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([
      { ...COMPLETED, parent_run_id: "orphan-parent" },
    ]);
    render(wrap(<RunsView />));
    await screen.findByText("summarize the week");
    expect(screen.getByText(/sub of orphan-parent/i)).toBeInTheDocument();
  });

  it("shows an empty state when there are no runs", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([]);
    render(wrap(<RunsView />));
    expect(await screen.findByText(/No runs match this filter/i)).toBeInTheDocument();
  });

  it("keeps a legacy (non-resumable) needs_input run out of the tray but in the feed", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, LEGACY_NEEDS_INPUT]);
    render(wrap(<RunsView />));

    // Feed shows both entries as ordinary history rows.
    await screen.findByText("set up the account");
    expect(screen.getByText("old paused run")).toBeInTheDocument();

    // Tray (built from strandedRuns) surfaces only the resumable one: its resume
    // form/textarea appears exactly once, not twice.
    expect(screen.getAllByPlaceholderText(/Type your answers/i)).toHaveLength(1);
  });

  it("polls while a run is still running", async () => {
    vi.useFakeTimers();
    // vi.restoreAllMocks() in afterEach only restores vi.spyOn spies, not the
    // vi.fn() mocks vi.mock()'s factory returns — clear this mock's call
    // history explicitly so counts from earlier tests in this file can't
    // leak in and make a broken poll implementation look like it's working.
    const listSpy = vi.mocked(listAllWorkflowRuns);
    listSpy.mockClear();
    listSpy.mockResolvedValue([
      { workflow: "wf", run_id: "r1", status: "running", started_at: 1, task: "t" },
    ] as never);

    render(wrap(<RunsView />));
    // Flush the mount effect's fetch before advancing the fake clock: at t=0
    // no interval exists yet (it's only created once `anyRunning` is known),
    // so there's nothing for advanceTimersByTimeAsync to iterate on until
    // this initial async render settles.
    await act(async () => {});
    await vi.advanceTimersByTimeAsync(5000);

    expect(listSpy.mock.calls.length).toBeGreaterThan(1);
    vi.useRealTimers();
  });

  it("stops polling once every run is finished", async () => {
    vi.useFakeTimers();
    const listSpy = vi.mocked(listAllWorkflowRuns);
    listSpy.mockClear();
    listSpy.mockResolvedValue([
      { workflow: "wf", run_id: "r1", status: "completed", started_at: 1, task: "t" },
    ] as never);

    render(wrap(<RunsView />));
    await act(async () => {});
    await vi.advanceTimersByTimeAsync(5000);
    const after = listSpy.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5000);

    expect(listSpy.mock.calls.length).toBe(after);
    vi.useRealTimers();
  });
});

describe("RunNodeRow", () => {
  it("renders duration, typical duration and artifacts on a node row", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <RunNodeRow
          continuesSession={false}
          typicalS={361}
          run={{
            node_id: "consolidate", iteration: 1, passed: null,
            session_key: "workflow:r1:consolidate:1", worker_index: null, branch_id: null,
            budget: null, status: "ok", route_label: null,
            duration_s: 361.5, artifacts: ["context.json"],
          }}
        />
      </I18nextProvider>,
    );

    expect(screen.getByText(/6:01/)).toBeInTheDocument();     // 361.5 s elapsed
    expect(screen.getByText(/context\.json/)).toBeInTheDocument();
  });
});
