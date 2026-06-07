import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SkillsView } from "@/components/SkillsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listSkills: vi.fn(),
    listQuarantine: vi.fn(),
    getSkill: vi.fn(),
    importSource: vi.fn(),
    approveSkill: vi.fn(),
    rejectSkill: vi.fn(),
    searchSkills: vi.fn(),
    judgeSkill: vi.fn(),
  };
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
  vi.mocked(api.importSource).mockReset();
  vi.mocked(api.approveSkill).mockReset();
  vi.mocked(api.rejectSkill).mockReset();
  vi.mocked(api.searchSkills).mockReset();
  vi.mocked(api.judgeSkill).mockReset();
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

  it("lists pending imports with their reason, and shows findings on triage", async () => {
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

    await user.click(screen.getByRole("button", { name: /pending/i }));

    // the queue shows the name and the workflow reason it's there
    expect(await screen.findByText("sketchy")).toBeInTheDocument();
    expect(screen.getByText(/awaiting your approval/i)).toBeInTheDocument();

    // the security findings live in the triage detail pane, opened on click
    await user.click(screen.getByRole("button", { name: /sketchy/i }));
    expect(await screen.findByText(/fetch-and-execute/)).toBeInTheDocument();
  });

  it("renders an empty state when nothing is pending", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");

    await user.click(screen.getByRole("button", { name: /pending/i }));

    expect(await screen.findByText("Nothing pending approval.")).toBeInTheDocument();
  });

  it("imports a source through the Add-skill acquire pane", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);
    vi.mocked(api.importSource).mockResolvedValue({
      quarantined: "imported", verdict: "safe", needs: "confirm", findings: [],
    });

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");

    // import lives behind the header "Add skill" action, not in the list
    await user.click(screen.getByRole("button", { name: /add skill/i }));
    const input = await screen.findByPlaceholderText(/Import a skill/i);
    await user.type(input, "github:owner/repo");
    await user.click(screen.getByRole("button", { name: "Import" }));

    expect(api.importSource).toHaveBeenCalledWith("tok", "github:owner/repo");
  });

  it("searches the registry and a hit's Import reuses the import-by-source flow", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);
    vi.mocked(api.searchSkills).mockResolvedValue({
      hits: [
        {
          name: "pdf-tools",
          ref: "github:acme/pdf-tools",
          registry: "acme",
          description: "Work with PDFs",
          signals: { installs: 42 },
        },
      ],
    });
    vi.mocked(api.importSource).mockResolvedValue({
      quarantined: "pdf-tools", verdict: "safe", needs: "confirm", findings: [],
    });

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");

    await user.click(screen.getByRole("button", { name: /add skill/i }));
    const box = await screen.findByPlaceholderText(/Search the registry/i);
    await user.type(box, "pdf");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(api.searchSkills).toHaveBeenCalledWith("tok", "pdf");
    // the hit renders with its name, install count and ref
    expect(await screen.findByText("pdf-tools")).toBeInTheDocument();
    expect(screen.getByText(/42 installs/)).toBeInTheDocument();
    expect(screen.getByText("github:acme/pdf-tools")).toBeInTheDocument();

    // the hit's Import button drives importSource with the hit's ref — the same
    // path the manual input uses (search itself never installs). Scope to the
    // hit's row so we don't catch the manual import input's Import button.
    const hitRow = screen
      .getByText("github:acme/pdf-tools")
      .closest("div.flex.items-start") as HTMLElement;
    await user.click(within(hitRow).getByRole("button", { name: "Import" }));

    expect(api.importSource).toHaveBeenCalledWith("tok", "github:acme/pdf-tools");
  });

  it("surfaces the install gate inline (no native dialog) and forces on confirm", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([
      { name: "evil", status: "quarantined", source: "github:o/r", verdict: "dangerous", findings: [] },
    ]);
    vi.mocked(api.approveSkill)
      .mockResolvedValueOnce({ refused: "block", verdict: "dangerous" })
      .mockResolvedValueOnce({ ok: true, name: "evil" });

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");
    await user.click(screen.getByRole("button", { name: /pending/i }));
    await user.click(await screen.findByRole("button", { name: /evil/i }));

    await user.click(await screen.findByRole("button", { name: "Approve" }));
    // an inline prompt appears (not window.confirm) — assert on a phrase unique
    // to the gate message, then force the install.
    expect(await screen.findByText(/serious risk/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Force install" }));

    expect(api.approveSkill).toHaveBeenNthCalledWith(1, "tok", "evil");
    expect(api.approveSkill).toHaveBeenNthCalledWith(2, "tok", "evil", {
      confirm: false,
      override: true,
      replace: false,
    });
  });

  it("offers an inline replace when a skill name already exists", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([
      { name: "dup", status: "quarantined", source: "github:o/r", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.approveSkill)
      .mockResolvedValueOnce({ refused: "exists", verdict: "safe" })
      .mockResolvedValueOnce({ ok: true, name: "dup" });

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");
    await user.click(screen.getByRole("button", { name: /pending/i }));
    await user.click(await screen.findByRole("button", { name: /dup/i }));

    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/already installed/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Replace" }));

    expect(api.approveSkill).toHaveBeenNthCalledWith(2, "tok", "dup", {
      confirm: false,
      override: false,
      replace: true,
    });
  });

  it("shows why-it's-here reasons in the triage pane", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([
      {
        name: "firecrawl", status: "quarantined", source: "github:o/r", verdict: "safe", findings: [],
        needs: "confirm",
        reasons: [{ code: "untrusted_source", detail: "github:o/r" }],
      },
    ]);

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");
    await user.click(screen.getByRole("button", { name: /pending/i }));
    await user.click(await screen.findByRole("button", { name: /firecrawl/i }));

    // why-it's-here renders the reason in plain language
    expect(await screen.findByText(/isn't in your trusted allowlist/i)).toBeInTheDocument();
  });

  it("streams audit reasoning then shows the final summary", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([
      { name: "firecrawl", status: "quarantined", source: "github:o/r", verdict: "safe", findings: [],
        needs: "confirm", reasons: [{ code: "untrusted_source", detail: "github:o/r" }] },
    ]);

    const handlers: Record<string, (ev: unknown) => void> = {};
    const client = {
      onChat: (id: string, h: (ev: unknown) => void) => {
        handlers[id] = h;
        return () => { delete handlers[id]; };
      },
      judgeStream: (name: string) => {
        const id = `audit:${name}`;
        handlers[id]?.({ event: "reasoning_delta", chat_id: id, text: "inspecting scripts" });
        handlers[id]?.({ event: "skill_audit_done", chat_id: id, name, judged: true,
          summary: "Reviewed; no injection.", findings: [], verdict: "safe" });
      },
    };

    const user = userEvent.setup();
    render(
      <ClientProvider client={client as unknown as import("@/lib/durin-client").DurinClient} token="tok">
        <SkillsView />
      </ClientProvider>,
    );
    await screen.findByText("clean");
    await user.click(screen.getByRole("button", { name: /pending/i }));
    await user.click(await screen.findByRole("button", { name: /firecrawl/i }));
    await user.click(screen.getByRole("button", { name: /audit with llm/i }));

    expect(await screen.findByText(/Reviewed; no injection/i)).toBeInTheDocument();
  });

  it("rejects a quarantined skill", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine)
      .mockResolvedValueOnce([
        { name: "sketchy", status: "quarantined", source: "github:o/r", verdict: "dangerous", findings: [] },
      ])
      .mockResolvedValue([]);
    vi.mocked(api.rejectSkill).mockResolvedValue({ ok: true });

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");
    await user.click(screen.getByRole("button", { name: /pending/i }));
    await user.click(await screen.findByRole("button", { name: /sketchy/i }));

    await user.click(await screen.findByRole("button", { name: "Reject" }));

    expect(api.rejectSkill).toHaveBeenCalledWith("tok", "sketchy");
  });
});
