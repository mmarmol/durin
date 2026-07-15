import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ModelPicker } from "@/components/settings/ModelPicker";

const fetchProviderModels = vi.fn();
const listModels = vi.fn();

vi.mock("@/lib/api", () => ({
  fetchProviderModels: (...a: unknown[]) => fetchProviderModels(...a),
  listModels: (...a: unknown[]) => listModels(...a),
}));

describe("ModelPicker (settings field)", () => {
  beforeEach(() => {
    fetchProviderModels.mockReset();
    listModels.mockReset();
  });

  it("shows the per-provider catalog and filters by capability", async () => {
    fetchProviderModels.mockResolvedValue([
      { id: "glm-5.2", configured: false, max_input_tokens: 1_000_000, supports_vision: false },
      { id: "glm-5v-turbo", configured: false, supports_vision: true },
    ]);
    render(
      <ModelPicker
        token="t"
        provider="zai_coding_plan"
        value=""
        onChange={() => {}}
        capability="vision"
      />,
    );
    fireEvent.focus(screen.getByRole("textbox"));
    await waitFor(() => screen.getByText("glm-5v-turbo"));
    // The vision role offers only the vision-capable model.
    expect(screen.queryByText("glm-5.2")).not.toBeInTheDocument();
    expect(fetchProviderModels).toHaveBeenCalledWith("t", "zai_coding_plan");
  });

  it("falls back to listModels when the provider is auto", async () => {
    listModels.mockResolvedValue({ suggested: ["gpt-5"], models: ["gpt-5", "gpt-4o"] });
    render(<ModelPicker token="t" provider="auto" value="" onChange={() => {}} />);
    fireEvent.focus(screen.getByRole("textbox"));
    await waitFor(() => screen.getByText("gpt-5"));
    expect(fetchProviderModels).not.toHaveBeenCalled();
    expect(listModels).toHaveBeenCalledWith("t", "auto", "");
  });

  it("renders long model ids in full (no CSS truncation class)", async () => {
    fetchProviderModels.mockResolvedValue([
      {
        id: "google/gemini-2.5-flash-lite-preview-06-17",
        configured: false,
        max_input_tokens: 1_000_000,
        supports_vision: true,
        supports_audio_input: true,
      },
      {
        id: "google/gemini-2.5-flash",
        configured: false,
        max_input_tokens: 1_000_000,
        supports_vision: true,
        supports_audio_input: true,
      },
    ]);
    render(<ModelPicker token="t" provider="openrouter" value="" onChange={() => {}} />);
    fireEvent.focus(screen.getByRole("textbox"));
    const long = await waitFor(() =>
      screen.getByText("google/gemini-2.5-flash-lite-preview-06-17"),
    );
    // The id element must not carry `truncate` — it wraps instead.
    expect(long.className).not.toContain("truncate");
    expect(long.className).toContain("break-all");
    // Both ids are present and distinct in the DOM.
    expect(screen.getByText("google/gemini-2.5-flash")).toBeInTheDocument();
  });
});
