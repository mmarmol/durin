import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowsView } from "@/components/WorkflowsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listWorkflows: vi.fn(),
    listPersonas: vi.fn(),
    getWorkflow: vi.fn(),
    getWorkflowRecommendations: vi.fn(),
    runWorkflow: vi.fn(),
    listWorkflowRuns: vi.fn(),
    getWorkflowRunManifest: vi.fn(),
  };
});

// happy-dom lacks ResizeObserver, which @xyflow/react instantiates on mount.
beforeEach(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  if (!navigator.clipboard) {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: () => Promise.resolve() },
      configurable: true,
    });
  }
  vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
  vi.mocked(api.listWorkflows).mockResolvedValue(["demo"]);
  vi.mocked(api.listPersonas).mockResolvedValue({ personas: [], default: null });
  vi.mocked(api.getWorkflow).mockResolvedValue({
    name: "demo",
    start: "start",
    nodes: [{ id: "start", kind: "work", mode: "build", prompt: "", next: null }],
  } as never);
  vi.mocked(api.getWorkflowRecommendations).mockResolvedValue([]);
  vi.mocked(api.listWorkflowRuns).mockResolvedValue([]);
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

const RUN_RESULT: api.WorkflowRunResult = {
  status: "completed",
  final_output: "the merged answer",
  run_id: "run-abc123",
  output_dir: "/work/out",
  runs: [
    {
      node_id: "research",
      iteration: 0,
      passed: true,
      output: "found the relevant evidence",
      session_key: "workflow:run-abc123:research",
      worker_index: null,
      branch_id: null,
      status: "ok",
      route_label: null,
    },
    {
      node_id: "fanout",
      iteration: 0,
      passed: null,
      output: "worker two output",
      session_key: "workflow:run-abc123:fanout-2",
      worker_index: 2,
      branch_id: null,
      status: "node_failed",
      route_label: null,
    },
  ],
};

async function openRunnerAndRun() {
  const user = userEvent.setup();
  render(wrap(<WorkflowsView />));
  // Wait for the workflow def to load (the runner toggle only shows with a def).
  const runner = await screen.findByText(/Test run/);
  await user.click(runner);
  const textarea = await screen.findByPlaceholderText(/Task to run/i);
  await user.type(textarea, "do the thing");
  await user.click(screen.getByRole("button", { name: /^Run$/i }));
  return user;
}

async function openRunner() {
  const user = userEvent.setup();
  render(wrap(<WorkflowsView />));
  const runner = await screen.findByText(/Test run/);
  await user.click(runner);
  return user;
}

describe("WorkflowsView run detail", () => {
  it("renders per-node status, output, and a copyable session affordance", async () => {
    vi.mocked(api.runWorkflow).mockResolvedValue(RUN_RESULT);
    await openRunnerAndRun();

    // Per-node identity + the (previously dropped) per-node output.
    await screen.findByText("research#0");
    expect(screen.getByText("found the relevant evidence")).toBeInTheDocument();
    expect(screen.getByText("worker two output")).toBeInTheDocument();

    // Status is surfaced for each node (ok + the failure status).
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();

    // Fan-out worker index is legible.
    expect(screen.getByText("worker 2")).toBeInTheDocument();

    // The session affordance surfaces each node's session key as a copyable reference.
    const copyButtons = screen.getAllByRole("button", { name: /copy session key/i });
    expect(copyButtons.length).toBe(2);
    expect(
      screen.getByText("workflow:run-abc123:research"),
    ).toBeInTheDocument();

    // Final output is still shown.
    expect(screen.getByText("the merged answer")).toBeInTheDocument();
  });

  it("copies the session key to the clipboard when its button is clicked", async () => {
    vi.mocked(api.runWorkflow).mockResolvedValue(RUN_RESULT);
    const user = await openRunnerAndRun();

    await screen.findByText("research#0");
    const first = screen.getAllByRole("button", { name: /copy session key/i })[0];
    await user.click(first);
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "workflow:run-abc123:research",
    );
  });

  it("keeps a run history that re-shows a past run's detail when clicked", async () => {
    const first = { ...RUN_RESULT, run_id: "run-1", final_output: "first answer" };
    const second = { ...RUN_RESULT, run_id: "run-2", final_output: "second answer" };
    vi.mocked(api.runWorkflow)
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);

    const user = await openRunnerAndRun();
    await screen.findByText("first answer");

    // Trigger a second run; history now has two entries and shows the latest.
    await user.click(screen.getByRole("button", { name: /^Run$/i }));
    await screen.findByText("second answer");

    const history = screen.getByText(/^runs$/i, { selector: "span" }).parentElement as HTMLElement;
    // Click the older run (#1) and assert its detail comes back.
    await user.click(within(history).getByRole("button", { name: "#1" }));
    expect(await screen.findByText("first answer")).toBeInTheDocument();
  });

  it("shows a needs_input banner with the resume form and resumes into the same run_id", async () => {
    const needsInput: api.WorkflowRunResult = {
      status: "needs_input",
      final_output: "What is your budget?",
      run_id: "run-pause",
      needs_input_node: "ask",
      runs: [],
    };
    const resumed: api.WorkflowRunResult = {
      status: "completed",
      final_output: "all done",
      run_id: "run-pause",
      runs: [],
    };
    vi.mocked(api.runWorkflow).mockResolvedValueOnce(needsInput).mockResolvedValueOnce(resumed);

    const user = await openRunnerAndRun();

    await screen.findByText("Waiting for your input");
    expect(screen.getByText(/What is your budget\?/)).toBeInTheDocument();

    const answers = screen.getByPlaceholderText(/Type your answers/i);
    await user.type(answers, "$500");
    await user.click(screen.getByRole("button", { name: /Resume run/i }));

    await screen.findByText("all done");
    // The resume call must carry the SAME run_id and the answers as the task.
    expect(api.runWorkflow).toHaveBeenLastCalledWith(
      "tok", "demo", "$500", [], "", "", "run-pause",
    );

    // History still has exactly one entry for this run (replaced, not appended).
    const history = screen.getByText(/^runs$/i, { selector: "span" }).parentElement as HTMLElement;
    expect(within(history).getAllByRole("button")).toHaveLength(1);
  });

  it("shows an aborted banner with the failure reason", async () => {
    vi.mocked(api.runWorkflow).mockResolvedValue({
      status: "aborted",
      final_output: "node 'gate' failed: boom",
      run_id: "run-abort",
      runs: [],
    });
    await openRunnerAndRun();
    await screen.findByText("Run aborted");
    expect(screen.getByText("node 'gate' failed: boom")).toBeInTheDocument();
  });

  it("shows a cancelled banner", async () => {
    vi.mocked(api.runWorkflow).mockResolvedValue({
      status: "cancelled",
      final_output: "",
      run_id: "run-cancel",
      runs: [],
    });
    await openRunnerAndRun();
    await screen.findByText("Run cancelled");
  });

  it("shows a loop-limit banner for an exhausted run", async () => {
    vi.mocked(api.runWorkflow).mockResolvedValue({
      status: "exhausted",
      final_output: "",
      run_id: "run-exhausted",
      exhausted_node: "gate",
      runs: [],
    });
    await openRunnerAndRun();
    await screen.findByText("Loop limit reached");
    expect(screen.getByText("gate")).toBeInTheDocument();
  });

  it("lists output files (capped at 20) for a completed run", async () => {
    const files = Array.from({ length: 22 }, (_, i) => `out/file-${i}.md`);
    vi.mocked(api.runWorkflow).mockResolvedValue({
      ...RUN_RESULT,
      output_files: files,
    });
    await openRunnerAndRun();
    await screen.findByText("out/file-0.md");
    expect(screen.getByText("out/file-19.md")).toBeInTheDocument();
    expect(screen.queryByText("out/file-20.md")).not.toBeInTheDocument();
    expect(screen.getByText(/and 2 more/)).toBeInTheDocument();
  });

  it("shows pass X of budget and a FINAL PASS chip for a looping node", async () => {
    vi.mocked(api.runWorkflow).mockResolvedValue({
      status: "completed",
      final_output: "done",
      run_id: "run-loop",
      runs: [
        {
          node_id: "gate",
          iteration: 2,
          passed: true,
          output: "looked good",
          session_key: "workflow:run-loop:gate:2",
          worker_index: null,
          branch_id: null,
          budget: 2,
          status: "ok",
          route_label: null,
        },
      ],
    });
    await openRunnerAndRun();
    await screen.findByText(/pass 2 of 2/);
    expect(screen.getByText("final pass")).toBeInTheDocument();
  });

  it("renders persisted history chips and shows a manifest's detail when one is clicked", async () => {
    vi.mocked(api.listWorkflowRuns).mockResolvedValue([
      {
        run_id: "run-old-2",
        status: "completed",
        started_at: 200,
        finished_at: 210,
        task: "second task",
        needs_input_node: null,
      },
      {
        run_id: "run-old-1",
        status: "needs_input",
        started_at: 100,
        finished_at: null,
        task: "first task",
        needs_input_node: "ask",
      },
    ]);
    vi.mocked(api.getWorkflowRunManifest).mockResolvedValue({
      status: "completed",
      final_output: "persisted answer",
      run_id: "run-old-2",
      // Persisted node records never carry per-node output text.
      runs: [
        {
          node_id: "research",
          iteration: 0,
          passed: true,
          session_key: "workflow:run-old-2:research",
          worker_index: null,
          branch_id: null,
          status: "ok",
          route_label: null,
        },
      ],
    });

    const user = await openRunner();
    const history = (await screen.findByText(/^runs$/i, { selector: "span" })).parentElement as HTMLElement;
    // Two persisted chips render newest-first: #2 is run-old-2, #1 is run-old-1.
    expect(within(history).getByRole("button", { name: "#2" })).toBeInTheDocument();
    const olderChip = within(history).getByRole("button", { name: "#1" });
    expect(olderChip).toBeInTheDocument();

    await user.click(within(history).getByRole("button", { name: "#2" }));

    expect(await screen.findByText("persisted answer")).toBeInTheDocument();
    // The node row renders without per-node output text (manifest records omit it).
    expect(screen.getByText("research#0")).toBeInTheDocument();
    expect(screen.queryByText("worker two output")).not.toBeInTheDocument();
    expect(api.getWorkflowRunManifest).toHaveBeenCalledWith("tok", "demo", "run-old-2");
  });

  it("shows the retention hint next to the history strip", async () => {
    vi.mocked(api.listWorkflowRuns).mockResolvedValue([
      {
        run_id: "run-old-1",
        status: "completed",
        started_at: 100,
        finished_at: 110,
        task: "a task",
        needs_input_node: null,
      },
    ]);
    await openRunner();
    await screen.findByText(/^runs$/i, { selector: "span" });
    expect(screen.getByText(/keep the most recent runs/i)).toBeInTheDocument();
  });

  it("marks a sub-run's history chip with its parent run in the tooltip", async () => {
    vi.mocked(api.listWorkflowRuns).mockResolvedValue([
      {
        run_id: "run-child-1",
        status: "completed",
        started_at: 100,
        finished_at: 110,
        task: "a task",
        needs_input_node: null,
        parent_run_id: "run-parent-1",
      },
    ]);
    await openRunner();
    const history = (await screen.findByText(/^runs$/i, { selector: "span" })).parentElement as HTMLElement;
    const chip = within(history).getByRole("button", { name: "#1" });
    expect(chip).toHaveAttribute("title", expect.stringContaining("· sub of run-parent-1"));
  });

  it("shows a breadcrumb naming the node that produced the final output", async () => {
    vi.mocked(api.runWorkflow).mockResolvedValue({
      ...RUN_RESULT,
      final_output_node: "summarize",
    });
    await openRunnerAndRun();
    await screen.findByText("the merged answer");
    expect(screen.getByText(/final output from summarize/i)).toBeInTheDocument();
  });
});
