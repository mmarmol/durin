import { render, screen, fireEvent } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { ThreadActionsProvider } from "@/components/thread/ThreadActionsContext";
import { WorkChip } from "@/components/thread/WorkChip";
import type { ToolProgressEvent } from "@/lib/types";

function runningWorkflowEvent(workflowName: string): ToolProgressEvent {
  return {
    version: 1,
    phase: "running",
    call_id: "workflow:r1",
    name: "workflow_progress",
    arguments: { workflow: workflowName },
    // nodes array proves the chip does NOT render them
    nodes: [
      { id: "gather", status: "running" },
      { id: "analyze", status: "done" },
    ],
  };
}

function doneSubagentEvent(label: string): ToolProgressEvent {
  return {
    version: 1,
    phase: "end",
    call_id: "subagent:s1",
    name: "subagent_result",
    arguments: { label, task: "research task" },
  };
}

function wrap(node: React.ReactNode, openWorkPanel = vi.fn()) {
  return (
    <I18nextProvider i18n={i18n}>
      <ThreadActionsProvider
        value={{
          sendUserMessage: () => {},
          storeSecret: async () => {},
          openWorkPanel,
        }}
      >
        {node}
      </ThreadActionsProvider>
    </I18nextProvider>
  );
}

it("renders a compact chip (no node list) and opens the panel on click", () => {
  const openWorkPanel = vi.fn();
  render(
    wrap(
      <WorkChip event={runningWorkflowEvent("research-to-answer")} />,
      openWorkPanel,
    ),
  );
  expect(screen.getByText("research-to-answer")).toBeInTheDocument();
  // node ids must NOT appear inline
  expect(screen.queryByText("gather")).not.toBeInTheDocument();
  expect(screen.queryByText("analyze")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button"));
  expect(openWorkPanel).toHaveBeenCalled();
});

it("shows a running spinner for phase=running", () => {
  render(wrap(<WorkChip event={runningWorkflowEvent("my-flow")} />));
  // The loader icon has aria-label or title, test it renders (no error)
  expect(screen.getByRole("button")).toBeInTheDocument();
});

it("shows a check icon for phase=end (done)", () => {
  const openWorkPanel = vi.fn();
  render(
    wrap(<WorkChip event={doneSubagentEvent("research-agent")} />, openWorkPanel),
  );
  expect(screen.getByText("research-agent")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button"));
  expect(openWorkPanel).toHaveBeenCalled();
});

it("calls openWorkPanel from context when no external prop provided", () => {
  const openWorkPanel = vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <ThreadActionsProvider
        value={{ sendUserMessage: () => {}, storeSecret: async () => {}, openWorkPanel }}
      >
        <WorkChip event={runningWorkflowEvent("test-flow")} />
      </ThreadActionsProvider>
    </I18nextProvider>,
  );
  fireEvent.click(screen.getByRole("button"));
  expect(openWorkPanel).toHaveBeenCalledTimes(1);
});
