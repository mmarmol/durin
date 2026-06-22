import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { SkillsSecuritySettings } from "@/components/settings/SkillsSecuritySettings";
import * as api from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getConfig: vi.fn(),
    listSecrets: vi.fn(),
    setConfigValue: vi.fn(),
    testGithubToken: vi.fn(),
  };
});

beforeEach(() => {
  vi.mocked(api.getConfig).mockReset();
  vi.mocked(api.listSecrets).mockReset();
  vi.mocked(api.setConfigValue).mockReset();
  vi.mocked(api.listSecrets).mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

it("renders the judge max-severity label translated, not the raw i18n key", async () => {
  vi.mocked(api.getConfig).mockResolvedValue({
    config: { skills: { security: { llmJudge: { trigger: "off", maxSeverity: "caution" } } } },
  } as never);
  render(<SkillsSecuritySettings token="tok" />);
  // The label must resolve to its translation (en default), never the raw key.
  expect(await screen.findByText("Maximum severity")).toBeInTheDocument();
  expect(
    screen.queryByText("settings.skillSecurity.rows.judgeMaxSeverity"),
  ).not.toBeInTheDocument();
  // The dropdown options must translate too.
  expect(screen.queryByText("settings.skillSecurity.severity.caution")).not.toBeInTheDocument();
});

it("lists discovery registries with a per-source toggle and writes the updated array", async () => {
  vi.mocked(api.getConfig).mockResolvedValue({
    config: {
      skills: {
        discovery: {
          registries: [
            { name: "skills.sh", kind: "skills.sh", enabled: true },
            { name: "clawhub", kind: "clawhub", enabled: true },
          ],
        },
      },
    },
  } as never);
  vi.mocked(api.setConfigValue).mockResolvedValue({ skills: {} } as never);

  const user = userEvent.setup();
  render(<SkillsSecuritySettings token="tok" />);

  // Both registries are listed (this is the "where do I enable clawhub vs skills.sh" surface).
  expect(await screen.findByText("skills.sh")).toBeInTheDocument();
  expect(screen.getByText("clawhub")).toBeInTheDocument();

  // Toggling clawhub off writes the full registries array with enabled flipped.
  await user.click(screen.getByRole("button", { name: /clawhub/i }));
  expect(api.setConfigValue).toHaveBeenCalledWith(
    "tok",
    "skills.discovery.registries",
    [
      { name: "skills.sh", kind: "skills.sh", enabled: true },
      { name: "clawhub", kind: "clawhub", enabled: false },
    ],
  );
});
