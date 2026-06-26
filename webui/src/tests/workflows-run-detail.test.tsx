import { render, screen, waitFor, within } from "@testing-library/react";
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
  vi.mocked(api.listPersonas).mockResolvedValue({ personas: [] });
  vi.mocked(api.getWorkflow).mockResolvedValue({
    name: "demo",
    start: "start",
    nodes: [{ id: "start", kind: "work", mode: "build", prompt: "", next: null }],
  } as never);
  vi.mocked(api.getWorkflowRecommendations).mockResolvedValue([]);
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
  await user.click(screen.getByRole("button", { name: /Run/i }));
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
    await user.click(screen.getByRole("button", { name: /Run/i }));
    await screen.findByText("second answer");

    const history = screen.getByText(/^runs$/i).parentElement as HTMLElement;
    // Click the older run (#1) and assert its detail comes back.
    await user.click(within(history).getByRole("button", { name: "#1" }));
    expect(await screen.findByText("first answer")).toBeInTheDocument();
  });
});
