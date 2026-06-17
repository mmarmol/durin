import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ProviderModelsSettings } from "@/components/settings/ProviderModelsSettings";

const fetchProviderModels = vi.fn();
const upsertProviderModel = vi.fn();
const removeProviderModel = vi.fn();

vi.mock("@/lib/api", () => ({
  fetchProviderModels: (...a: unknown[]) => fetchProviderModels(...a),
  upsertProviderModel: (...a: unknown[]) => upsertProviderModel(...a),
  removeProviderModel: (...a: unknown[]) => removeProviderModel(...a),
}));

describe("ProviderModelsSettings", () => {
  beforeEach(() => {
    fetchProviderModels.mockReset().mockResolvedValue([
      { id: "glm-5.2", configured: false, max_input_tokens: 1_000_000, supports_reasoning: true },
      { id: "my-custom", configured: true },
    ]);
    upsertProviderModel.mockReset().mockResolvedValue(undefined);
    removeProviderModel.mockReset().mockResolvedValue(undefined);
  });

  it("lists provider models with caps after expanding", async () => {
    render(<ProviderModelsSettings token="t" provider="zai_coding_plan" label="Z.ai" />);
    fireEvent.click(screen.getByText("Z.ai"));
    await waitFor(() => screen.getByText("glm-5.2"));
    expect(screen.getByText(/1M/)).toBeInTheDocument();
    expect(fetchProviderModels).toHaveBeenCalledWith("t", "zai_coding_plan");
  });

  it("adds a custom model id", async () => {
    render(<ProviderModelsSettings token="t" provider="zai_coding_plan" label="Z.ai" />);
    fireEvent.click(screen.getByText("Z.ai"));
    await waitFor(() => screen.getByText("glm-5.2"));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "new-model" } });
    fireEvent.click(screen.getByRole("button", { name: /add|agregar/i }));
    await waitFor(() =>
      expect(upsertProviderModel).toHaveBeenCalledWith("t", "zai_coding_plan", "new-model", {}),
    );
  });
});
