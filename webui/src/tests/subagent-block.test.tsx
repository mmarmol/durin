import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HoistedToolBlock } from "@/components/thread/ToolBlocks";

/** subagent_result now renders a compact WorkChip, not an inline detail block.
 *  All detail (progress, result, error) is in the work panel. */
describe("SubagentResultBlock live", () => {
  it("shows label chip while running (no step count inline)", () => {
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
    // Step count and task text are not shown inline — they live in the work panel
    expect(screen.queryByText(/do x/)).not.toBeInTheDocument();
    expect(screen.getByRole("button")).toBeInTheDocument();
  });

  it("shows label chip when ended (result not inline)", () => {
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
    expect(screen.getByText(/research/)).toBeInTheDocument();
    // Result text is not shown inline — it lives in the work panel
    expect(screen.queryByText("all done")).not.toBeInTheDocument();
    expect(screen.getByRole("button")).toBeInTheDocument();
  });
});
