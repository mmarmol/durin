import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsView } from "@/components/settings/SettingsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";
import type { SettingsPayload } from "@/lib/types";

// The judge knob is a (model, provider) pair like every aux row — a bare
// model name resolved on the default provider produced silent 404s (the
// live glm-5-turbo incident). The row must offer a provider picker and
// persist BOTH fields.

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchSettings: vi.fn(),
    getConfig: vi.fn(),
    setConfigValue: vi.fn(),
    listModels: vi.fn(async () => ({ models: [], suggested: [] })),
    fetchProviderModels: vi.fn(async () => []),
    getModelCapabilities: vi.fn(async () => ({
      supports_vision: false,
      supports_audio_input: false,
      max_input_tokens: 0,
    })),
    testModel: vi.fn(async () => ({ status: "ok", message: "ok", fix: "" })),
  };
});

const SETTINGS: SettingsPayload = {
  agent: { model: "main-model", provider: "openai", resolved_provider: "openai", has_api_key: true },
  providers: [
    { name: "openai", label: "OpenAI", configured: true },
    { name: "ollama", label: "Ollama", configured: true, is_local: true },
  ],
  web_search: { provider: "duckduckgo", providers: [{ name: "duckduckgo", label: "DuckDuckGo", credential: "none" }] },
  runtime: { config_path: "/tmp/config.json" },
  requires_restart: false,
};

const fakeClient = {
  status: "open" as const,
  onStatus: (_cb: (status: string) => void) => () => {},
} as unknown as import("@/lib/durin-client").DurinClient;

function wrap(children: ReactNode) {
  return (
    <ClientProvider client={fakeClient} token="tok">
      {children}
    </ClientProvider>
  );
}

function renderSettings() {
  return render(
    wrap(
      <SettingsView
        theme="light"
        onToggleTheme={() => {}}
        palette="ithildin"
        onSelectPalette={() => {}}
        onBackToChat={() => {}}
        onModelNameChange={() => {}}
      />,
    ),
  );
}

async function judgeRow(): Promise<HTMLElement> {
  renderSettings();
  const rowTitle = await screen.findByText("Judge model");
  const row = rowTitle.closest("div.flex.min-h-\\[62px\\]") as HTMLElement;
  expect(row).toBeTruthy();
  return row;
}

beforeEach(() => {
  vi.mocked(api.fetchSettings).mockReset().mockResolvedValue(SETTINGS);
  vi.mocked(api.getConfig).mockReset().mockResolvedValue({
    config: { agents: { aux_models: {} }, skills: { security: { llm_judge: {} } } },
    schema: {},
  } as never);
  vi.mocked(api.setConfigValue).mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe("Judge model settings row", () => {
  it("offers a provider picker (the pair travels together)", async () => {
    const row = await judgeRow();
    expect(within(row).getByText(/pick a provider/i)).toBeInTheDocument();
  });

  it("saving a picked judge model writes model AND provider", async () => {
    vi.mocked(api.setConfigValue).mockResolvedValue({
      skills: { security: { llm_judge: { model: "glm-4.6", provider: "ollama" } } },
    } as never);
    const user = userEvent.setup();
    const row = await judgeRow();

    await user.click(within(row).getByText(/pick a provider/i));
    await user.click(await screen.findByText("Ollama"));
    await user.type(within(row).getByPlaceholderText(/model id/i), "glm-4.6");
    await user.click(within(row).getByRole("button", { name: /^save$/i }));

    await waitFor(() => {
      expect(api.setConfigValue).toHaveBeenCalledWith(
        "tok", "skills.security.llm_judge.model", "glm-4.6");
      expect(api.setConfigValue).toHaveBeenCalledWith(
        "tok", "skills.security.llm_judge.provider", "ollama");
    });
  });

  it("clearing resets the pair (model empty, provider auto)", async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      config: {
        agents: { aux_models: {} },
        skills: { security: { llm_judge: { model: "glm-5-turbo", provider: "auto" } } },
      },
      schema: {},
    } as never);
    vi.mocked(api.setConfigValue).mockResolvedValue({
      skills: { security: { llm_judge: {} } },
    } as never);
    const user = userEvent.setup();
    const row = await judgeRow();

    await user.click(within(row).getByRole("button", { name: /clear/i }));
    await waitFor(() => {
      expect(api.setConfigValue).toHaveBeenCalledWith(
        "tok", "skills.security.llm_judge.model", "");
      expect(api.setConfigValue).toHaveBeenCalledWith(
        "tok", "skills.security.llm_judge.provider", "auto");
    });
  });

  it("a legacy bare-name knob (provider auto) prompts for a provider pick", async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      config: {
        agents: { aux_models: {} },
        skills: { security: { llm_judge: { model: "glm-5-turbo", provider: "auto" } } },
      },
      schema: {},
    } as never);
    const row = await judgeRow();
    // AuxControl treats "auto" as no selection so the operator is nudged to
    // pick a real provider for the existing model name.
    expect(within(row).getByText(/pick a provider/i)).toBeInTheDocument();
  });
});
