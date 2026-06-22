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
