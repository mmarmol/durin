import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RunDetail } from "@/components/workflows/RunDetail";
import type { WorkflowRunNode, WorkflowRunResult } from "@/lib/api";

// i18n is initialized globally in src/tests/setup.ts — no wrapper needed.

function node(extra: Partial<WorkflowRunNode> = {}): WorkflowRunNode {
  return {
    node_id: "analyze",
    iteration: 1,
    status: "ok",
    passed: null,
    route_label: null,
    session_key: "workflow:r1:analyze:1",
    worker_index: null,
    branch_id: null,
    budget: null,
    duration_s: 3,
    artifacts: [],
    ...extra,
  };
}

function result(extra: Partial<WorkflowRunResult> = {}): WorkflowRunResult {
  return {
    run_id: "r1",
    status: "completed",
    final_output: "",
    runs: [node()],
    ...extra,
  };
}

describe("opening a node from the executions pane", () => {
  it("asks the caller to open the node whose identity was clicked", async () => {
    const onOpenNode = vi.fn();
    render(
      <RunDetail result={result()} onResume={() => {}} resuming={false} onOpenNode={onOpenNode} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /analyze/ }));
    await waitFor(() => expect(onOpenNode).toHaveBeenCalledTimes(1));
    expect(onOpenNode.mock.calls[0][0].node_id).toBe("analyze");
  });

  it("leaves the row inert when no handler is provided", () => {
    render(<RunDetail result={result()} onResume={() => {}} resuming={false} />);
    expect(screen.queryByRole("button", { name: /analyze/ })).not.toBeInTheDocument();
  });

  it("opens the node that is still in flight, carrying the session it is writing", async () => {
    const onOpenNode = vi.fn();
    render(
      <RunDetail
        result={result({
          status: "running",
          runs: [],
          active_node: {
            node_id: "analyze",
            label: "Analyze",
            started_at: 1_000,
            iteration: 2,
            session_key: "workflow:r1:analyze:2",
          },
        })}
        onResume={() => {}}
        resuming={false}
        onOpenNode={onOpenNode}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Analyze/ }));
    await waitFor(() => expect(onOpenNode).toHaveBeenCalledTimes(1));
    // The row it hands over is synthesized from the marker — the run has no
    // completed row for this pass yet, but the marker names the session.
    expect(onOpenNode.mock.calls[0][0].session_key).toBe("workflow:r1:analyze:2");
    expect(onOpenNode.mock.calls[0][0].iteration).toBe(2);
  });

  it("leaves an in-flight node with no session inert", () => {
    // A script node in flight: it persists no conversation, so there is nothing
    // to open until its row lands with the captured streams.
    render(
      <RunDetail
        result={result({
          status: "running",
          runs: [],
          active_node: {
            node_id: "check",
            label: "Check",
            started_at: 1_000,
            iteration: 1,
            session_key: null,
          },
        })}
        onResume={() => {}}
        resuming={false}
        onOpenNode={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /Check/ })).not.toBeInTheDocument();
  });
});
