import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { ToolCallBlock } from "@/components/thread/ToolCallBlock";
import type { ToolProgressEvent } from "@/lib/types";

function renderBlock(event: ToolProgressEvent) {
  return render(
    <I18nextProvider i18n={i18n}>
      <ToolCallBlock event={event} />
    </I18nextProvider>,
  );
}

it("collapses the run_workflow node dump to its first line", () => {
  const event: ToolProgressEvent = {
    version: 1,
    phase: "end",
    call_id: "workflow:abc",
    name: "run_workflow",
    arguments: { name: "research-to-answer" },
    result:
      "Workflow run abc: completed\n" +
      "  [plan#1] -> workflow:abc:plan:1\n" +
      "  [search#1] -> workflow:abc:search:1:0\n" +
      "  [gather#1] -> workflow:abc:gather:1",
  };
  renderBlock(event);
  // The status summary (first line) shows; the per-node dump is collapsed.
  expect(screen.getByText(/Workflow run abc: completed/)).toBeInTheDocument();
  expect(screen.queryByText(/\[plan#1\]/)).not.toBeInTheDocument();
  expect(screen.queryByText(/workflow:abc:gather/)).not.toBeInTheDocument();
});

it("leaves the generic tool preview at six lines (run_workflow change is scoped)", () => {
  const result = Array.from({ length: 10 }, (_, i) => `line ${i + 1}`).join("\n");
  const event: ToolProgressEvent = {
    version: 1,
    phase: "end",
    call_id: "x",
    name: "read_file",
    arguments: { path: "a.txt" },
    result,
  };
  renderBlock(event);
  expect(screen.getByText("line 6")).toBeInTheDocument();
  expect(screen.queryByText("line 7")).not.toBeInTheDocument();
});
