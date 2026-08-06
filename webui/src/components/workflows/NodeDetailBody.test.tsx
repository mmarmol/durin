import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NodeDetailBody } from "@/components/workflows/NodeDetailBody";
import type { WorkflowRunNode } from "@/lib/api";
import type { UIMessage } from "@/lib/types";

// i18n is initialized globally in src/tests/setup.ts — no wrapper needed.

function row(extra: Partial<WorkflowRunNode> = {}): WorkflowRunNode {
  return {
    node_id: "analyze",
    iteration: 1,
    status: "ok",
    passed: null,
    route_label: null,
    session_key: null,
    worker_index: null,
    branch_id: null,
    budget: null,
    exit_code: null,
    duration_s: 4.2,
    artifacts: [],
    ...extra,
  };
}

describe("NodeDetailBody", () => {
  it("shows a script node's command, exit code and both streams", () => {
    render(
      <NodeDetailBody
        row={row({
          command: "./check.sh",
          exit_code: 2,
          stdout: "checked 3 files",
          stderr: "missing header",
        })}
        transcript={null}
        transcriptState="idle"
      />,
    );
    expect(screen.getByText("./check.sh")).toBeInTheDocument();
    expect(screen.getByText(/checked 3 files/)).toBeInTheDocument();
    expect(screen.getByText(/missing header/)).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("renders a script node that printed nothing without empty stream blocks", () => {
    render(
      <NodeDetailBody
        row={row({ command: "true", exit_code: 0, stdout: "", stderr: "" })}
        transcript={null}
        transcriptState="idle"
      />,
    );
    // An empty stream is absent, not an empty labelled box the reader has to
    // parse before discovering it says nothing.
    expect(screen.queryByTestId("node-stdout")).not.toBeInTheDocument();
    expect(screen.queryByTestId("node-stderr")).not.toBeInTheDocument();
  });

  it("renders an agent node's transcript", () => {
    const transcript: UIMessage[] = [
      { id: "hist-0", role: "user", content: "Analyze the diff", createdAt: 1 },
      { id: "hist-1", role: "assistant", content: "Two files changed.", createdAt: 2 },
    ];
    render(
      <NodeDetailBody
        row={row({ session_key: "workflow:r1:analyze:1" })}
        transcript={transcript}
        transcriptState="idle"
      />,
    );
    expect(screen.getByText("Analyze the diff")).toBeInTheDocument();
    expect(screen.getByText("Two files changed.")).toBeInTheDocument();
  });

  it("shows the live activity line while the node is working", () => {
    render(
      <NodeDetailBody
        row={row({ session_key: "workflow:r1:analyze:1" })}
        activity={{ tool: "read_file", target: "src/app.ts", at: 1 }}
        round={3}
        maxRounds={10}
        transcript={[]}
        transcriptState="idle"
      />,
    );
    expect(screen.getByText(/src\/app\.ts/)).toBeInTheDocument();
    expect(screen.getByTestId("node-round")).toHaveTextContent("3");
  });

  it("degrades to the header when the node kept no detail", () => {
    // A sub-workflow or parallel aggregate row: no session, no script streams.
    render(<NodeDetailBody row={row()} transcript={null} transcriptState="idle" />);
    expect(screen.getByText("analyze")).toBeInTheDocument();
    expect(screen.getByTestId("node-no-detail")).toBeInTheDocument();
  });

  it("says so when a node's session could not be read", () => {
    render(
      <NodeDetailBody
        row={row({ session_key: "workflow:r1:analyze:1" })}
        transcript={null}
        transcriptState="missing"
      />,
    );
    expect(screen.getByTestId("node-transcript-missing")).toBeInTheDocument();
  });

  it("does not offer a transcript section for a script node", () => {
    // A script node has no session at all: a "what this step did" heading with
    // nothing under it would read as a record that failed to load.
    render(
      <NodeDetailBody
        row={row({ command: "echo hi", exit_code: 0, stdout: "hi" })}
        transcript={null}
        transcriptState="idle"
      />,
    );
    expect(screen.queryByTestId("node-transcript-missing")).not.toBeInTheDocument();
    expect(screen.queryByTestId("node-no-detail")).not.toBeInTheDocument();
  });
});
