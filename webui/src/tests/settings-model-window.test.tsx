import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { SettingsView } from "@/components/settings/SettingsView";
import * as api from "@/lib/api";
import type { SettingsPayload } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchSettings: vi.fn(),
    getConfig: vi.fn(),
    setConfigValue: vi.fn(),
    listModels: vi.fn(async () => ({ models: [], suggested: [] })),
    fetchProviderModels: vi.fn(async () => []),
    getModelCapabilities: vi.fn(),
    testModel: vi.fn(async () => ({ status: "ok", message: "ok", fix: "" })),
  };
});

const SETTINGS: SettingsPayload = {
  agent: {
    model: "glm-5.3",
    provider: "zai_coding_plan",
    resolved_provider: "zai_coding_plan",
    has_api_key: true,
  },
  providers: [{ name: "zai_coding_plan", label: "Z.ai Coding Plan", configured: true }],
  web_search: {
    provider: "duckduckgo",
    providers: [{ name: "duckduckgo", label: "DuckDuckGo", credential: "none" }],
  },
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

beforeEach(() => {
  vi.mocked(api.fetchSettings).mockReset().mockResolvedValue(SETTINGS);
  vi.mocked(api.getConfig).mockReset().mockResolvedValue({
    config: { agents: { aux_models: {} } },
    schema: {},
  } as never);
  vi.mocked(api.getModelCapabilities).mockReset().mockResolvedValue({
    model: "glm-5.3",
    // The model can take 1M; this config caps runs on it at 231,072.
    max_input_tokens: 1_000_000,
    context_window_tokens: 231_072,
    supports_vision: false,
    supports_audio_input: false,
    supports_function_calling: true,
  });
});
afterEach(() => vi.restoreAllMocks());

it("the default model line shows the window its runs get, not the model's own limit", async () => {
  render(
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
  await screen.findByText("Default model");
  expect((await screen.findAllByText(/231K context/)).length).toBeGreaterThan(0);
  expect(screen.queryByText(/1000K context/)).not.toBeInTheDocument();
});
