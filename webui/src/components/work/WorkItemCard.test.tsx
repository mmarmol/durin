import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { WorkItem } from "@/lib/types";
import { WorkItemCard } from "./WorkItemCard";

// i18n is initialized globally in src/tests/setup.ts — no wrapper needed.

describe("WorkItemCard", () => {
  it("renders parallel branches nested under the running node", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r1",
          label: "research-to-answer",
          status: "running",
          nodes: [
            {
              id: "gather",
              status: "running",
              branches: [
                { id: "search", status: "done" },
                { id: "search", status: "running" },
              ],
            },
          ],
          startedAt: 0,
          endedAt: null,
        }}
      />,
    );
    expect(screen.getByText("gather")).toBeInTheDocument();
    expect(screen.getAllByText("search")).toHaveLength(2);
  });

  it("renders the workflow label in the header", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "wf1",
          label: "my-workflow",
          status: "done",
          nodes: [],
          startedAt: 0,
          endedAt: 100,
        }}
      />,
    );
    expect(screen.getByText("my-workflow")).toBeInTheDocument();
  });

  it("renders step count for a subagent item", () => {
    render(
      <WorkItemCard
        item={{
          kind: "subagent",
          id: "sa1",
          label: "my-agent",
          status: "running",
          steps: 7,
          startedAt: 0,
          endedAt: null,
        }}
      />,
    );
    expect(screen.getByText("my-agent")).toBeInTheDocument();
    // work.steps with count=7 renders as "7 steps"
    expect(screen.getByText("7 steps")).toBeInTheDocument();
  });

  it("renders failed status with error tone", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "wf2",
          label: "broken",
          status: "failed",
          nodes: [{ id: "step1", status: "failed" }],
          startedAt: 0,
          endedAt: 50,
        }}
      />,
    );
    expect(screen.getByText("broken")).toBeInTheDocument();
    expect(screen.getByText("step1")).toBeInTheDocument();
  });

  it("renders needs_input status marker", () => {
    const item: WorkItem = {
      kind: "workflow",
      id: "wf3",
      label: "waiting",
      status: "needs_input",
      nodes: [],
      startedAt: 0,
      endedAt: null,
    };
    render(<WorkItemCard item={item} />);
    expect(screen.getByText("waiting")).toBeInTheDocument();
    expect(screen.getByText("Needs input")).toBeInTheDocument();
  });

  it("renders task as prominent title with workflow name as secondary tag", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r2",
          label: "research-workflow",
          task: "summarise the quarterly earnings report",
          status: "running",
          nodes: [],
          startedAt: 0,
          endedAt: null,
        }}
      />,
    );
    // task text is the title
    expect(screen.getByText("summarise the quarterly earnings report")).toBeInTheDocument();
    // workflow name is the secondary tag
    expect(screen.getByText("research-workflow")).toBeInTheDocument();
  });

  it("falls back to workflow name as title when task is absent", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r3",
          label: "my-workflow",
          status: "done",
          nodes: [],
          startedAt: 0,
          endedAt: 100,
        }}
      />,
    );
    expect(screen.getByText("my-workflow")).toBeInTheDocument();
  });

  it("renders node label instead of raw id when label is present", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r4",
          label: "research-to-answer",
          status: "running",
          nodes: [
            {
              id: "plan",
              label: "Break the question into research angles",
              status: "done",
            },
            {
              id: "gather",
              label: "Collect and synthesize results",
              status: "running",
              branches: [
                { id: "br1", label: "Search angle A", status: "done" },
                { id: "br2", label: "Search angle B", status: "running" },
              ],
            },
          ],
          startedAt: 0,
          endedAt: null,
        }}
      />,
    );
    // labels are rendered instead of raw ids
    expect(screen.getByText("Break the question into research angles")).toBeInTheDocument();
    expect(screen.getByText("Collect and synthesize results")).toBeInTheDocument();
    expect(screen.getByText("Search angle A")).toBeInTheDocument();
    expect(screen.getByText("Search angle B")).toBeInTheDocument();
    // raw ids must NOT be rendered
    expect(screen.queryByText("plan")).not.toBeInTheDocument();
    expect(screen.queryByText("gather")).not.toBeInTheDocument();
  });

  it("falls back to node id when label is absent", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r5",
          label: "wf",
          status: "done",
          nodes: [{ id: "step1", status: "done" }],
          startedAt: 0,
          endedAt: 10,
        }}
      />,
    );
    expect(screen.getByText("step1")).toBeInTheDocument();
  });

  it("renders pass chip for looping node on second+ iteration", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r6",
          label: "looping-wf",
          status: "running",
          nodes: [
            {
              id: "looper",
              status: "running",
              iteration: 2,
              budget: 3,
            },
          ],
          startedAt: 0,
          endedAt: null,
        }}
      />,
    );
    // Expects "pass 2 of 3" based on en/common.json passOf template
    expect(screen.getByText("pass 2 of 3")).toBeInTheDocument();
  });

  it("renders needs_input hand-off hint in workflow card", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r7",
          label: "handed-off",
          status: "needs_input",
          nodes: [],
          startedAt: 0,
          endedAt: null,
        }}
      />,
    );
    // Expects the hint text based on en/common.json needsInputHint
    expect(
      screen.getByText(
        "Handed to the calling agent — it may answer from its own context or ask you, then resume this run.",
      ),
    ).toBeInTheDocument();
  });

  it("renders the questions box when needsInputDetail is present", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r8",
          label: "waiting",
          status: "needs_input",
          needsInputDetail: "Which env — staging or prod?",
          nodes: [],
          startedAt: 0,
          endedAt: null,
        }}
      />,
    );
    expect(screen.getByText("Questions")).toBeInTheDocument();
    expect(screen.getByText("Which env — staging or prod?")).toBeInTheDocument();
  });

  it("does not render the questions box when needsInputDetail is absent", () => {
    render(
      <WorkItemCard
        item={{
          kind: "workflow",
          id: "r9",
          label: "waiting",
          status: "needs_input",
          nodes: [],
          startedAt: 0,
          endedAt: null,
        }}
      />,
    );
    expect(screen.queryByText("Questions")).not.toBeInTheDocument();
  });

  it("shows the round and the activity for the running node only", () => {
    render(<WorkItemCard item={{
      kind: "workflow", id: "r1", label: "ticket-stage1-context", status: "running",
      // The run's own clock (footer) must read distinctly from the running
      // node's clock below: this run also spent 120s on the finished node
      // before the still-running one started 261s ago, so its total is older.
      startedAt: Date.now() - 381_000, endedAt: null,
      nodes: [
        { id: "resolve-org", label: "Resolve org", status: "done", durationS: 120 },
        // round/maxRounds is the agent-round axis — distinct from iteration/budget
        // (the node's visit count), which PassChip renders. Not the same "10".
        { id: "consolidate", label: "Consolidate", status: "running", startedAt: Date.now() / 1000 - 261,
          round: 3, maxRounds: 10, activity: { tool: "read_file", target: "investigation.json", at: 0 } },
      ],
    }} />);

    expect(screen.getByText("2:00")).toBeInTheDocument();       // finished node duration
    expect(screen.getByText(/4:2\d/)).toBeInTheDocument();       // running node clock
    // Exact, not a loose /3.*10/: the round line's placeholders must match what
    // the call site binds, or an unbound one renders as literal "{{...}}".
    expect(screen.getByText("round 3 of 10")).toBeInTheDocument();
    expect(screen.getByText(/investigation\.json/)).toBeInTheDocument();
  });

  it("shows no activity line for a finished node", () => {
    render(<WorkItemCard item={{
      kind: "workflow", id: "r1", label: "wf", status: "done",
      startedAt: 0, endedAt: 1,
      nodes: [{ id: "a", label: "A", status: "done", durationS: 3,
                activity: { tool: "read_file", target: "stale.json", at: 0 } }],
    }} />);
    expect(screen.queryByText(/stale\.json/)).not.toBeInTheDocument();
  });

  it("renders pending nodes muted and without a clock", () => {
    render(<WorkItemCard item={{
      kind: "workflow", id: "r1", label: "wf", status: "running",
      startedAt: Date.now() - 1000, endedAt: null,
      nodes: [
        { id: "consolidate", label: "Consolidate", status: "running", startedAt: Date.now() / 1000 - 5 },
        { id: "report", label: "Report", status: "pending" },
      ],
    }} />);
    const pending = screen.getByText("Report");
    expect(pending).toBeInTheDocument();
    expect(pending.parentElement?.textContent).not.toMatch(/\d:\d\d/);
  });

  it("counts the nodes the run has touched, with no denominator", () => {
    render(<WorkItemCard item={{
      kind: "workflow", id: "r1", label: "wf", status: "running",
      startedAt: Date.now() - 1000, endedAt: null,
      nodes: [
        { id: "a", label: "A", status: "done", durationS: 1 },
        { id: "b", label: "B", status: "running", startedAt: Date.now() / 1000 },
        { id: "c", label: "C", status: "pending" },
      ],
    }} />);
    // Two touched (done + running); the pending tail is not a promise and is not counted.
    expect(screen.getByText(/2 nodes/)).toBeInTheDocument();
    expect(screen.queryByText(/of 3/)).not.toBeInTheDocument();
  });

  it("offers the node's own sentence as hover text on its label", () => {
    render(<WorkItemCard item={{
      kind: "workflow", id: "r1", label: "wf", status: "running",
      startedAt: Date.now() - 1000, endedAt: null,
      nodes: [{
        id: "judge", label: "Judge", status: "running",
        startedAt: Date.now() / 1000,
        description: "You are the JUDGE — the final quality gate before a note reaches the customer",
      }],
    }} />);
    expect(screen.getByText("Judge")).toHaveAttribute(
      "title",
      "You are the JUDGE — the final quality gate before a note reaches the customer",
    );
  });

  it("rails in a node that belongs to a nested sub-workflow run", () => {
    render(<WorkItemCard item={{
      kind: "workflow", id: "r1", label: "wf", status: "running",
      startedAt: Date.now() - 1000, endedAt: null,
      nodes: [
        { id: "own", label: "Own", status: "done", durationS: 1 },
        { id: "nested", label: "Nested", status: "running",
          startedAt: Date.now() / 1000, parentNode: "call-child" },
      ],
    }} />);
    // The nested node's row is indented and railed; the run's own node's is not.
    expect(screen.getByText("Nested").parentElement?.className).toMatch(/border-l/);
    expect(screen.getByText("Own").parentElement?.className).not.toMatch(/border-l/);
  });
});
