import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { HoistedToolBlock } from "@/components/thread/ToolBlocks";

it("renders workflow nodes with status", () => {
  render(<I18nextProvider i18n={i18n}><HoistedToolBlock answered={false} event={{
    version: 1, phase: "running", call_id: "workflow:r1", name: "workflow_progress",
    arguments: { workflow: "qa" },
    nodes: [{ id: "lint", status: "done" }, { id: "test", status: "running" }],
  } as any} /></I18nextProvider>);
  expect(screen.getByText("lint")).toBeInTheDocument();
  expect(screen.getByText("test")).toBeInTheDocument();
});
