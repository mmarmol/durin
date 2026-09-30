import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ConfigSettings } from "@/components/settings/ConfigSettings";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getConfig: vi.fn(), setConfigValue: vi.fn() };
});

function wrap(children: ReactNode) {
  return (
    <ClientProvider
      client={{} as unknown as import("@/lib/durin-client").DurinClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(api.getConfig).mockReset();
  vi.mocked(api.setConfigValue).mockReset();
});
afterEach(() => vi.restoreAllMocks());

it("hides the legacy loops section from the editor while still showing its sibling sections", async () => {
  vi.mocked(api.getConfig).mockResolvedValue({
    config: { loops: { keep_runs: 5 }, automations: { keep_runs: 20 } },
  } as never);

  render(wrap(<ConfigSettings token="tok" />));

  await screen.findByText("automations");
  expect(screen.queryByText("loops")).not.toBeInTheDocument();
});

it("writes a model entry whose name has dots under a bracketed key", async () => {
  // A dotted path would split `glm-5.3` into `glm-5` + `3`: the save landed in
  // a new, unrelated `glm-5` entry instead of the model's own.
  vi.mocked(api.getConfig).mockResolvedValue({
    config: {
      providers: {
        zai_coding_plan: { models: { "glm-5.3": { context_window_tokens: 231072 } } },
      },
    },
  } as never);
  vi.mocked(api.setConfigValue).mockResolvedValue({} as never);

  const user = userEvent.setup();
  render(wrap(<ConfigSettings token="tok" />));
  await user.click(await screen.findByText("providers"));

  const input = screen.getByDisplayValue("231072");
  await user.clear(input);
  await user.type(input, "200000");
  await user.click(screen.getByRole("button", { name: "Save" }));

  expect(api.setConfigValue).toHaveBeenCalledWith(
    "tok",
    'providers.zai_coding_plan.models["glm-5.3"].context_window_tokens',
    200000,
  );
});

it("renders an array config value clipped (not overflowing) with a full-value tooltip", async () => {
  const arr = ["github:anthropics/", "github:openai/"];
  const json = JSON.stringify(arr);
  vi.mocked(api.getConfig).mockResolvedValue({
    config: { skills: { security: { allowlist: arr } } },
  } as never);

  const user = userEvent.setup();
  render(wrap(<ConfigSettings token="tok" />));

  // expand the "skills" group
  await user.click(await screen.findByText("skills"));

  const cell = screen.getByText(json);
  // truncate is only effective on a block-level box — an inline <span> ignores
  // max-width/overflow, which is exactly what made the allowlist overlap its label.
  expect(cell.className).toMatch(/\b(inline-block|block)\b/);
  expect(cell).toHaveClass("truncate");
  // the full value stays reachable on hover instead of sprawling across the row
  expect(cell).toHaveAttribute("title", json);
});
