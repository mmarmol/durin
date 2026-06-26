import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HoistedToolBlock } from "@/components/thread/ToolBlocks";

describe("SubagentResultBlock live", () => {
  it("shows running progress with step count", () => {
    render(
      <HoistedToolBlock
        answered={false}
        event={
          {
            version: 1,
            phase: "running",
            call_id: "subagent:t1",
            name: "subagent_result",
            arguments: { label: "research", task: "do x" },
            progress: { iteration: 3, tool: "grep" },
          } as any
        }
      />,
    );
    expect(screen.getByText(/research/)).toBeInTheDocument();
    expect(screen.getByText(/3/)).toBeInTheDocument();
  });

  it("shows the result when ended", () => {
    render(
      <HoistedToolBlock
        answered={false}
        event={
          {
            version: 1,
            phase: "end",
            call_id: "subagent:t1",
            name: "subagent_result",
            arguments: { label: "research", task: "do x" },
            result: "all done",
          } as any
        }
      />,
    );
    expect(screen.getByText("all done")).toBeInTheDocument();
  });
});
