import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ModelSelectField } from "@/components/ModelSelectField";

const fetchModelPicker = vi.fn();

vi.mock("@/lib/api", () => ({
  fetchModelPicker: (...a: unknown[]) => fetchModelPicker(...a),
}));

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ token: "tok" }),
}));

// Stub ModelPickerPopover so we can control what it renders without
// triggering the real popover's fetchModelPicker call (that's tested
// separately in model-picker-popover.test.tsx).
const mockOnSelect = vi.fn();
vi.mock("@/components/thread/ModelPickerPopover", () => ({
  ModelPickerPopover: ({
    open,
    onSelect,
    activeModel,
  }: {
    open: boolean;
    onSelect: (ref: string) => void;
    activeModel: string | null;
  }) => {
    if (!open) return null;
    return (
      <div data-testid="mock-popover" data-active={activeModel ?? ""}>
        <button
          type="button"
          onClick={() => {
            mockOnSelect(onSelect);
            onSelect("provider/some-model");
          }}
        >
          pick-model
        </button>
      </div>
    );
  },
}));

describe("ModelSelectField", () => {
  beforeEach(() => {
    fetchModelPicker.mockReset().mockResolvedValue([
      { name: "GLM 5", provider: "zai", group: "general", role: "agent", ref: "zai/glm-5" },
    ]);
    mockOnSelect.mockReset();
  });

  it("shows 'default model' label when value is empty string", () => {
    render(<ModelSelectField value="" onChange={() => {}} />);
    // The trigger button should show the default label (i18n key: settings.cron.modelDefault).
    // In tests i18n returns the key itself, so we match "modelDefault" or the actual label.
    const trigger = screen.getByRole("button", { name: /model/i });
    expect(trigger).toBeInTheDocument();
    // No clear button when value is "".
    expect(screen.queryByTitle(/default/i)).not.toBeInTheDocument();
  });

  it("shows the resolved display name when value is a known ref", async () => {
    render(<ModelSelectField value="zai/glm-5" onChange={() => {}} />);
    // fetchModelPicker resolves, then the button label updates.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /model/i }).textContent).toContain("GLM 5"),
    );
  });

  it("falls back to the raw ref when the ref is not found in entries", async () => {
    render(<ModelSelectField value="unknown/model" onChange={() => {}} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /model/i }).textContent).toContain("unknown/model"),
    );
  });

  it("opens the popover when the trigger button is clicked", async () => {
    render(<ModelSelectField value="" onChange={() => {}} />);
    expect(screen.queryByTestId("mock-popover")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /model/i }));
    expect(screen.getByTestId("mock-popover")).toBeInTheDocument();
  });

  it("calls onChange with the selected ref and closes popover", async () => {
    const onChange = vi.fn();
    render(<ModelSelectField value="" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: /model/i }));
    await waitFor(() => screen.getByTestId("mock-popover"));
    fireEvent.click(screen.getByText("pick-model"));
    expect(onChange).toHaveBeenCalledWith("provider/some-model");
    // Popover closes after selection.
    expect(screen.queryByTestId("mock-popover")).not.toBeInTheDocument();
  });

  it("shows a clear button when a model is selected and resets to default on click", async () => {
    const onChange = vi.fn();
    render(<ModelSelectField value="zai/glm-5" onChange={onChange} />);
    await waitFor(() => screen.getByRole("button", { name: /model/i }));
    // Clear button (X) should be present.
    const clearBtn = screen.getByRole("button", { name: /default/i });
    fireEvent.click(clearBtn);
    expect(onChange).toHaveBeenCalledWith("");
  });
});
