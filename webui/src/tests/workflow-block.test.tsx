import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { HoistedToolBlock } from "@/components/thread/ToolBlocks";

it("renders a compact work chip (no node list) for workflow_progress", () => {
  render(<I18nextProvider i18n={i18n}><HoistedToolBlock answered={false} event={{
    version: 1, phase: "running", call_id: "workflow:r1", name: "workflow_progress",
    arguments: { workflow: "qa" },
    nodes: [{ id: "lint", status: "done" }, { id: "test", status: "running" }],
  } as any} /></I18nextProvider>);
  // Shows the workflow name
  expect(screen.getByText("qa")).toBeInTheDocument();
  // Does NOT show the node list — that detail is in the work panel
  expect(screen.queryByText("lint")).not.toBeInTheDocument();
  expect(screen.queryByText("test")).not.toBeInTheDocument();
  // Renders as a clickable button chip
  expect(screen.getByRole("button")).toBeInTheDocument();
});
