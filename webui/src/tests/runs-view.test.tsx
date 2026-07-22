import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import { RunDetail, RunNodeRow } from "@/components/workflows/RunDetail";
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

  it("refreshes an open running run's manifest on each poll and reflects the newer nodes", async () => {
    vi.useFakeTimers();
    // Both are vi.fn()s from vi.mock()'s factory, which this file's afterEach
    // (vi.restoreAllMocks()) never resets — clear explicitly so call counts left
    // over from earlier tests can't leak into these assertions.
    const listSpy = vi.mocked(listAllWorkflowRuns);
    const manifestSpy = vi.mocked(api.getWorkflowRunManifest);
    listSpy.mockClear();
    manifestSpy.mockClear();
    listSpy.mockResolvedValue([
      { workflow: "wf", run_id: "r1", status: "running", started_at: 1, task: "watch me" },
    ] as never);
    manifestSpy
      .mockResolvedValueOnce({
        status: "running",
        final_output: "",
        run_id: "r1",
        runs: [
          { node_id: "step-one", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
        ],
      })
      .mockResolvedValue({
        status: "running",
        final_output: "",
        run_id: "r1",
        runs: [
          { node_id: "step-one", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
          { node_id: "step-two", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
        ],
      });

    render(wrap(<RunsView />));
    await act(async () => {});
    fireEvent.click(screen.getByText("watch me"));
    await act(async () => {});

    expect(manifestSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/step-one/)).toBeInTheDocument();
    expect(screen.queryByText(/step-two/)).not.toBeInTheDocument();

    await vi.advanceTimersByTimeAsync(5000);

    expect(manifestSpy.mock.calls.length).toBeGreaterThan(1);
    expect(screen.getByText(/step-two/)).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("does not re-fetch a finished run's manifest even while the poll keeps ticking for other runs", async () => {
    vi.useFakeTimers();
    const listSpy = vi.mocked(listAllWorkflowRuns);
    const manifestSpy = vi.mocked(api.getWorkflowRunManifest);
    listSpy.mockClear();
    manifestSpy.mockClear();
    // r1 (opened below) is already finished; r2 is still running, so the shared
    // poll interval stays alive and keeps ticking regardless of r1's own detail.
    listSpy.mockResolvedValue([
      { workflow: "wf", run_id: "r1", status: "completed", started_at: 1, task: "already done" },
      { workflow: "wf", run_id: "r2", status: "running", started_at: 2, task: "still going" },
    ] as never);
    manifestSpy.mockResolvedValue({
      status: "completed",
      final_output: "final",
      run_id: "r1",
      runs: [
        { node_id: "only-step", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
      ],
    });

    render(wrap(<RunsView />));
    await act(async () => {});
    fireEvent.click(screen.getByText("already done"));
    await act(async () => {});

    expect(manifestSpy).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(9000);

    // The list poll genuinely fired (proving the interval is alive)...
    expect(listSpy.mock.calls.length).toBeGreaterThan(1);
    // ...but the finished run's manifest was not touched again.
    expect(manifestSpy).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("stops refreshing an open run's manifest after unmount", async () => {
    vi.useFakeTimers();
    const listSpy = vi.mocked(listAllWorkflowRuns);
    const manifestSpy = vi.mocked(api.getWorkflowRunManifest);
    listSpy.mockClear();
    manifestSpy.mockClear();
    listSpy.mockResolvedValue([
      { workflow: "wf", run_id: "r1", status: "running", started_at: 1, task: "watch me" },
    ] as never);
    manifestSpy.mockResolvedValue({
      status: "running",
      final_output: "",
      run_id: "r1",
      runs: [
        { node_id: "step-one", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
      ],
    });

    const { unmount } = render(wrap(<RunsView />));
    await act(async () => {});
    fireEvent.click(screen.getByText("watch me"));
    await act(async () => {});
    await vi.advanceTimersByTimeAsync(4000);

    const listCallsBeforeUnmount = listSpy.mock.calls.length;
    const manifestCallsBeforeUnmount = manifestSpy.mock.calls.length;
    expect(listCallsBeforeUnmount).toBeGreaterThan(1);
    expect(manifestCallsBeforeUnmount).toBeGreaterThan(1);

    unmount();
    await vi.advanceTimersByTimeAsync(20000);

    expect(listSpy.mock.calls.length).toBe(listCallsBeforeUnmount);
    expect(manifestSpy.mock.calls.length).toBe(manifestCallsBeforeUnmount);
    vi.useRealTimers();
  });

  it("keeps a newly selected run's manifest when a previous run's poll refresh resolves late", async () => {
    vi.useFakeTimers();
    const listSpy = vi.mocked(listAllWorkflowRuns);
    const manifestSpy = vi.mocked(api.getWorkflowRunManifest);
    listSpy.mockClear();
    manifestSpy.mockClear();
    listSpy.mockResolvedValue([
      { workflow: "wf", run_id: "r1", status: "running", started_at: 1, task: "run A" },
      { workflow: "wf", run_id: "r2", status: "running", started_at: 2, task: "run B" },
    ] as never);

    const aInitial: api.WorkflowRunResult = {
      status: "running",
      final_output: "",
      run_id: "r1",
      runs: [
        { node_id: "a-step", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
      ],
    };
    const bManifest: api.WorkflowRunResult = {
      status: "running",
      final_output: "",
      run_id: "r2",
      runs: [
        { node_id: "b-step", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
      ],
    };
    // What A's poll re-fetch eventually resolves to — distinguishable from
    // bManifest so an incorrect overwrite is detectable.
    const aStaleLate: api.WorkflowRunResult = {
      status: "running",
      final_output: "",
      run_id: "r1",
      runs: [
        { node_id: "a-step", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
        { node_id: "a-step-late", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
      ],
    };

    // Resolved by hand below, after B is already selected — simulating a poll
    // tick's re-fetch of A that takes longer than the user's next click.
    let resolveAPoll!: (v: api.WorkflowRunResult) => void;
    const aPollPromise = new Promise<api.WorkflowRunResult>((res) => {
      resolveAPoll = res;
    });

    manifestSpy
      .mockResolvedValueOnce(aInitial) // 1: click on A
      .mockImplementationOnce(() => aPollPromise) // 2: poll tick's re-fetch of A, held open
      .mockResolvedValueOnce(bManifest); // 3: click on B

    render(wrap(<RunsView />));
    await act(async () => {});

    fireEvent.click(screen.getByText("run A"));
    await act(async () => {});
    expect(screen.getByText(/a-step/)).toBeInTheDocument();

    // Poll tick fires: refreshOpenManifest re-fetches A's manifest (mock call
    // #2 above) and leaves it unresolved for now.
    await vi.advanceTimersByTimeAsync(4000);

    // Before that re-fetch resolves, the user switches to run B.
    fireEvent.click(screen.getByText("run B"));
    await act(async () => {});
    expect(screen.getByText(/b-step/)).toBeInTheDocument();

    // A's stale poll response finally arrives.
    resolveAPoll(aStaleLate);
    await act(async () => {});

    // It must not have clobbered B's already-rendered detail.
    expect(screen.getByText(/b-step/)).toBeInTheDocument();
    expect(screen.queryByText(/a-step-late/)).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("keeps a newly selected run's manifest when a previous run's own fetch resolves late", async () => {
    // No polling involved here — this isolates the click-driven path
    // (onSelectEntry) from the poll-driven one covered above, so both rows
    // are already-finished runs and no interval is ever created.
    const listSpy = vi.mocked(listAllWorkflowRuns);
    const manifestSpy = vi.mocked(api.getWorkflowRunManifest);
    listSpy.mockClear();
    manifestSpy.mockClear();
    listSpy.mockResolvedValue([
      { workflow: "wf", run_id: "r1", status: "completed", started_at: 1, task: "run A" },
      { workflow: "wf", run_id: "r2", status: "completed", started_at: 2, task: "run B" },
    ] as never);

    const bManifest: api.WorkflowRunResult = {
      status: "completed",
      final_output: "b done",
      run_id: "r2",
      runs: [
        { node_id: "b-step", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
      ],
    };
    const aLate: api.WorkflowRunResult = {
      status: "completed",
      final_output: "a done",
      run_id: "r1",
      runs: [
        { node_id: "a-step", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null },
      ],
    };

    // Resolved by hand below, after B is already selected — simulating A's
    // own click-driven fetch taking longer than the user's next click.
    let resolveA!: (v: api.WorkflowRunResult) => void;
    const aPromise = new Promise<api.WorkflowRunResult>((res) => {
      resolveA = res;
    });

    manifestSpy
      .mockImplementationOnce(() => aPromise) // 1: click on A, held open
      .mockResolvedValueOnce(bManifest); // 2: click on B, resolves first

    render(wrap(<RunsView />));
    await act(async () => {});

    fireEvent.click(screen.getByText("run A"));
    await act(async () => {});
    // A's own fetch is still pending — nothing to assert yet.

    fireEvent.click(screen.getByText("run B"));
    await act(async () => {});
    expect(screen.getByText(/b-step/)).toBeInTheDocument();

    // A's abandoned fetch finally resolves.
    resolveA(aLate);
    await act(async () => {});

    // It must not have clobbered B's already-rendered detail.
    expect(screen.getByText(/b-step/)).toBeInTheDocument();
    expect(screen.queryByText(/a-step/)).not.toBeInTheDocument();
  });
});

describe("RunDetail", () => {
  function renderDetail(result: api.WorkflowRunResult) {
    return render(
      <I18nextProvider i18n={i18n}>
        <RunDetail result={result} onResume={() => {}} resuming={false} />
      </I18nextProvider>,
    );
  }

  const RUNNING: api.WorkflowRunResult = {
    status: "running",
    final_output: "",
    run_id: "r1",
    runs: [],
    active_node: { node_id: "consolidate", label: "Consolidate", started_at: 1000 },
  };

  it("shows the in-flight node of a running run", () => {
    renderDetail(RUNNING);
    expect(screen.getByText("Consolidate")).toBeInTheDocument();
  });

  it("does not report a crashed run's last node as still running", () => {
    // Crash reconciliation flips the status without clearing active_node, so an
    // ungated read spins forever on a node whose process died long ago.
    renderDetail({ ...RUNNING, status: "crashed" });
    expect(screen.queryByText("Consolidate")).not.toBeInTheDocument();
  });

  it("takes the typical total from the run's own estimate, not the sum of node medians", () => {
    // The per-node medians cover both branches of a router; a run takes one.
    renderDetail({
      status: "completed",
      final_output: "",
      run_id: "r2",
      runs: [{
        node_id: "branch-a", iteration: 1, passed: null, session_key: null,
        worker_index: null, branch_id: null, budget: null, status: "ok",
        route_label: null, duration_s: 500,
      }],
      typical_s: { "branch-a": 500, "branch-b": 520 },
      typical_total_s: 515,
    });

    expect(screen.getByText(/8:35/)).toBeInTheDocument();     // the measured median total
    expect(screen.queryByText(/17:00/)).not.toBeInTheDocument();  // the summed branches
  });

  it("omits the typical total for a workflow with no completed-run history", () => {
    renderDetail({ status: "completed", final_output: "", run_id: "r3", runs: [] });
    expect(screen.queryByText(/prior runs/)).not.toBeInTheDocument();
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
