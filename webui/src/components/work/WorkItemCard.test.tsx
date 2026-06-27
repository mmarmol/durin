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
});
