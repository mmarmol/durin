import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsView } from "@/components/settings/SettingsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";
import type { SettingsPayload } from "@/lib/types";

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
        palette="violet"
        onSelectPalette={() => {}}
        onBackToChat={() => {}}
        onModelNameChange={() => {}}
      />,
    ),
  );
}

beforeEach(() => {
  vi.mocked(api.fetchSettings).mockReset().mockResolvedValue(SETTINGS);
  vi.mocked(api.getConfig).mockReset().mockResolvedValue({
    config: { agents: { aux_models: {} } },
    schema: {},
  } as never);
  vi.mocked(api.setConfigValue).mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe("Subagent model settings row", () => {
  it("renders directly below the default model row when aux subagents is unset", async () => {
    renderSettings();
    await screen.findByText("Default model");
    const subagentRow = await screen.findByText("Subagent model");
    expect(subagentRow).toBeInTheDocument();
    // Placement: the subagent row is the row immediately after the default
    // model row (both live in the same AI section, subagents first among
    // the aux rows).
    const rows = screen.getAllByText(/Default model|Subagent model|Vision model/);
    const order = rows.map((el) => el.textContent);
    expect(order.indexOf("Default model")).toBeLessThan(order.indexOf("Subagent model"));
    expect(order.indexOf("Subagent model")).toBeLessThan(order.indexOf("Vision model"));
  });

  it("shows no value (same as default) when agents.aux_models.subagents is unset", async () => {
    renderSettings();
    await screen.findByText("Subagent model");
    // AuxControl with no `current` shows no Clear button.
    expect(screen.queryByRole("button", { name: /clear/i })).not.toBeInTheDocument();
  });

  it("shows a Clear (same as default) control when a subagent aux model is configured", async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      config: {
        agents: { aux_models: { subagents: { model: "cheap-model", provider: "ollama" } } },
      },
      schema: {},
    } as never);
    renderSettings();
    await screen.findByText("Subagent model");
    expect(await screen.findByRole("button", { name: /clear/i })).toBeInTheDocument();
  });

  it("clearing the subagent model writes null to agents.aux_models.subagents", async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      config: {
        agents: { aux_models: { subagents: { model: "cheap-model", provider: "ollama" } } },
      },
      schema: {},
    } as never);
    vi.mocked(api.setConfigValue).mockResolvedValue({
      agents: { aux_models: {} },
    } as never);
    const user = userEvent.setup();
    renderSettings();
    await screen.findByText("Subagent model");
    const clearButton = await screen.findByRole("button", { name: /clear/i });
    await user.click(clearButton);
    await waitFor(() =>
      expect(api.setConfigValue).toHaveBeenCalledWith("tok", "agents.aux_models.subagents", null),
    );
  });

  it("saving a picked subagent model writes {model, provider} to agents.aux_models.subagents", async () => {
    vi.mocked(api.setConfigValue).mockResolvedValue({
      agents: { aux_models: { subagents: { model: "cheap-model", provider: "ollama" } } },
    } as never);
    const user = userEvent.setup();
    renderSettings();
    const rowTitle = await screen.findByText("Subagent model");
    const row = rowTitle.closest("div.flex.min-h-\\[62px\\]") as HTMLElement;
    expect(row).toBeTruthy();

    await user.click(within(row).getByText(/pick a provider/i));
    await user.click(await screen.findByText("Ollama"));

    await user.type(within(row).getByPlaceholderText(/model id/i), "cheap-model");
    await user.click(within(row).getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(api.setConfigValue).toHaveBeenCalledWith(
        "tok",
        "agents.aux_models.subagents",
        { model: "cheap-model", provider: "ollama" },
      ),
    );
  });
});
