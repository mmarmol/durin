// webui/src/tests/formula-actions.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FormulaActions } from "@/components/math/FormulaActions";

const writeText = vi.fn().mockResolvedValue(undefined);
const write = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  writeText.mockClear();
  write.mockClear();
  // happy-dom exposes clipboard as a getter-only property; use defineProperty.
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText, write },
  });
  // happy-dom lacks ClipboardItem; provide a minimal stub that records data.
  (globalThis as unknown as { ClipboardItem: unknown }).ClipboardItem =
    class {
      data: Record<string, unknown>;
      constructor(data: Record<string, unknown>) {
        this.data = data;
      }
    };
});

afterEach(() => vi.restoreAllMocks());

function renderFormula() {
  return render(
    <FormulaActions>
      <span className="katex">
        <span className="katex-mathml">
          <math>
            <annotation encoding="application/x-tex">E = mc^2</annotation>
          </math>
        </span>
        <span className="katex-html">E</span>
      </span>
    </FormulaActions>,
  );
}

describe("FormulaActions", () => {
  it("copies the LaTeX source", async () => {
    renderFormula();
    fireEvent.click(screen.getByRole("button", { name: "Copy LaTeX" }));
    expect(writeText).toHaveBeenCalledWith("E = mc^2");
  });

  it("copies MathML markup for Word", async () => {
    renderFormula();
    fireEvent.click(screen.getByRole("button", { name: "Copy for Word" }));
    expect(write).toHaveBeenCalledTimes(1);
  });
});
