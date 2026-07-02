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
});
