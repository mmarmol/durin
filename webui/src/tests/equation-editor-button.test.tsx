// webui/src/tests/equation-editor-button.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EquationEditorButton } from "@/components/math/EquationEditorButton";

describe("EquationEditorButton", () => {
  it("renders a button that opens the editor dialog", async () => {
    render(<EquationEditorButton onInsert={vi.fn()} />);
    const btn = screen.getByRole("button", { name: "Equation editor" });
    fireEvent.click(btn);
    expect(await screen.findByText("Write an equation")).toBeInTheDocument();
  });

  it("wraps the entered value in $…$ and calls onInsert", async () => {
    const onInsert = vi.fn();
    render(<EquationEditorButton onInsert={onInsert} />);
    fireEvent.click(screen.getByRole("button", { name: "Equation editor" }));
    // The dialog seeds the field value via a test hook input.
    const field = await screen.findByLabelText("equation-field");
    fireEvent.input(field, { target: { value: "x^2" } });
    fireEvent.click(screen.getByRole("button", { name: "Insert" }));
    expect(onInsert).toHaveBeenCalledWith("$x^2$");
  });
});
