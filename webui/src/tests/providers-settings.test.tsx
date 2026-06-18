import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ProvidersSettings } from "@/components/settings/ProvidersSettings";
import type { SettingsPayload } from "@/lib/types";

const fetchProviderModels = vi.fn();
const updateProviderSettings = vi.fn();
const upsertProviderModel = vi.fn();
const removeProviderModel = vi.fn();

vi.mock("@/lib/api", () => ({
  fetchProviderModels: (...a: unknown[]) => fetchProviderModels(...a),
  updateProviderSettings: (...a: unknown[]) => updateProviderSettings(...a),
  upsertProviderModel: (...a: unknown[]) => upsertProviderModel(...a),
  removeProviderModel: (...a: unknown[]) => removeProviderModel(...a),
}));

const SETTINGS: SettingsPayload = {
  agent: {
    model: "glm-5.2",
    provider: "zai_coding_plan",
    resolved_provider: "zai_coding_plan",
    has_api_key: true,
  },
  providers: [
    {
      name: "zai_coding_plan",
      label: "Z.ai Coding Plan",
      configured: true,
      api_key_hint: "sk-••••",
      api_base: "https://api.z.ai/api/coding/paas/v4",
    },
    { name: "anthropic", label: "Anthropic", configured: false },
  ],
  web_search: { provider: "brave", providers: [] },
  runtime: { config_path: "/tmp/config.json" },
  requires_restart: false,
};

describe("ProvidersSettings", () => {
  beforeEach(() => {
    fetchProviderModels.mockReset().mockResolvedValue([
      { id: "glm-5.2", configured: false, max_input_tokens: 1_000_000, supports_reasoning: true },
      { id: "glm-5v-turbo", configured: false, supports_vision: true },
    ]);
    updateProviderSettings.mockReset().mockResolvedValue(SETTINGS);
    upsertProviderModel.mockReset().mockResolvedValue(undefined);
    removeProviderModel.mockReset().mockResolvedValue(undefined);
  });

  it("lists configured and unconfigured providers with their status", async () => {
    render(<ProvidersSettings token="t" settings={SETTINGS} onRefresh={() => {}} />);
    expect(screen.getByText("Z.ai Coding Plan")).toBeInTheDocument();
    expect(screen.getByText("Anthropic")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Connect")).toBeInTheDocument();
    // model count badge appears once the provider's catalog loads
    await waitFor(() => expect(screen.getByText(/2 models/)).toBeInTheDocument());
    expect(fetchProviderModels).toHaveBeenCalledWith("t", "zai_coding_plan");
  });

  it("expands a provider to show connection + models with capabilities together", async () => {
    render(<ProvidersSettings token="t" settings={SETTINGS} onRefresh={() => {}} />);
    await waitFor(() => expect(fetchProviderModels).toHaveBeenCalled());
    fireEvent.click(screen.getByText("Z.ai Coding Plan"));

    await waitFor(() => screen.getByText("glm-5.2"));
    // models present, with capability text — not "no models"
    expect(screen.getByText("glm-5v-turbo")).toBeInTheDocument();
    expect(screen.getByText(/1M ctx/)).toBeInTheDocument();
    // connection lives in the same expanded panel
    expect(screen.getByText("sk-••••")).toBeInTheDocument();
    expect(screen.getByText("https://api.z.ai/api/coding/paas/v4")).toBeInTheDocument();
  });

  it("adds a custom model under the expanded provider", async () => {
    render(<ProvidersSettings token="t" settings={SETTINGS} onRefresh={() => {}} />);
    await waitFor(() => expect(fetchProviderModels).toHaveBeenCalled());
    fireEvent.click(screen.getByText("Z.ai Coding Plan"));
    await waitFor(() => screen.getByText("glm-5.2"));

    fireEvent.change(screen.getByPlaceholderText(/Add a model id/i), {
      target: { value: "glm-experimental" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add/i }));
    await waitFor(() =>
      expect(upsertProviderModel).toHaveBeenCalledWith("t", "zai_coding_plan", "glm-experimental", {}),
    );
  });
});
