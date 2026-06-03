import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SkillsView } from "@/components/SkillsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, listSkills: vi.fn(), listQuarantine: vi.fn(), getSkill: vi.fn() };
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
  vi.mocked(api.listSkills).mockReset();
  vi.mocked(api.listQuarantine).mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe("SkillsView security surface", () => {
  it("shows a verdict badge on a non-safe active skill and none on a safe one", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      {
        name: "evil",
        source: "workspace",
        mode: "manual",
        status: "active",
        verdict: "dangerous",
        findings: [
          { category: "prompt_injection", severity: "dangerous", where: "SKILL.md", detail: "ignore-previous-instructions" },
        ],
      },
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);

    render(wrap(<SkillsView />));

    expect(await screen.findByText("evil")).toBeInTheDocument();
    expect(screen.getByText("Dangerous")).toBeInTheDocument();
  });

  it("lists quarantined skills with their reasons under the Quarantine tab", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([
      {
        name: "sketchy",
        status: "quarantined",
        source: "github:owner/repo",
        verdict: "dangerous",
        findings: [
          { category: "dangerous_code", severity: "dangerous", where: "scripts/go.sh", detail: "fetch-and-execute (curl|bash)" },
        ],
      },
    ]);

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");

    await user.click(screen.getByRole("button", { name: /quarantine/i }));

    expect(await screen.findByText("sketchy")).toBeInTheDocument();
    expect(screen.getByText(/fetch-and-execute/)).toBeInTheDocument();
  });

  it("renders an empty state when nothing is quarantined", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");

    await user.click(screen.getByRole("button", { name: /quarantine/i }));

    expect(await screen.findByText("No skills in quarantine.")).toBeInTheDocument();
  });
});
