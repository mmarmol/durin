import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PendingView } from "@/components/PendingView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listPending: vi.fn(),
    decideApproval: vi.fn(),
    runWorkflow: vi.fn(),
    resolveFlaggedPair: vi.fn(),
    acceptSkillSuggestion: vi.fn(),
    rejectSkillSuggestion: vi.fn(),
    answerAutomationRun: vi.fn(),
    stopAutomationRun: vi.fn(),
  };
});

type Item = api.PendingItem;

function item(source: string, id: string, data: Record<string, unknown>, extra: Partial<Item> = {}): Item {
  return {
    source,
    id,
    kind: "k",
    title: id,
    summary: "",
    created_at: "2026-09-26T10:00:00+00:00",
    resolve: { form: source, actions: [] },
    data,
    ...extra,
  };
}

const APPROVAL = item("approval", "a1b2c3d4e5f6", {
  approval_id: "a1b2c3d4e5f6",
  kind: "skill_install",
  summary: "install skill 'mailer' from github:acme/mailer",
  detail: { verdict: "caution" },
}, { kind: "skill_install" });

const QUARANTINE = item("skill_quarantine", "imported", {
  name: "imported",
  status: "quarantined",
  source: "github:acme/imported",
  verdict: "caution",
  findings: [],
});

const AUTOMATION = item("automation_run", "arun1", {
  automation: "invoice-reminder",
  run_id: "arun1",
  status: "paused",
  ask_kind: "question",
  ask: "Which mailbox should I use?",
  started_at: 1_000,
});

const WORKFLOW = item("workflow_run", "wrun1", {
  workflow: "triage",
  run_id: "wrun1",
  status: "needs_input",
  needs_input_node: "ask",
  questions: "Which label applies?",
  started_at: 1_000,
});

const PAIR = item("flagged_pair", "person:ana|person:ana-lopez", {
  ref_a: "person:ana",
  ref_b: "person:ana-lopez",
  verdict: "same",
  confidence: 70,
  reasoning: "same email address",
  at_ms: 1_000,
});

const SUGGESTION = item("skill_suggestion", "sug1", {
  id: "sug1",
  skill: "mailer",
  type: "evolve",
  reason: "clearer steps",
  patch: null,
  created_at: "2026-09-26T10:00:00+00:00",
});

function list(items: Item[], errors: api.PendingList["errors"] = []): api.PendingList {
  return { items, count: items.length, errors };
}

function wrap(children: ReactNode) {
  return (
    <ClientProvider client={{} as unknown as import("@/lib/durin-client").DurinClient} token="tok">
      {children}
    </ClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(() => vi.restoreAllMocks());

describe("PendingView", () => {
  it("groups every item under its source, in a fixed order", async () => {
    vi.mocked(api.listPending).mockResolvedValue(
      list([SUGGESTION, WORKFLOW, APPROVAL, PAIR, AUTOMATION, QUARANTINE]),
    );

    render(wrap(<PendingView />));

    const headings = await screen.findAllByRole("heading", { level: 2 });
    expect(headings.map((h) => h.textContent)).toEqual([
      "Approvals · 1",
      "Skill imports · 1",
      "Automations · 1",
      "Workflow runs · 1",
      "Memory pairs · 1",
      "Skill suggestions · 1",
    ]);
    const approvals = screen.getByRole("region", { name: "Approvals" });
    expect(within(approvals).getByText("install skill 'mailer' from github:acme/mailer")).toBeInTheDocument();
    const automations = screen.getByRole("region", { name: "Automations" });
    expect(within(automations).getByTestId("inbox-card")).toBeInTheDocument();
    const workflows = screen.getByRole("region", { name: "Workflow runs" });
    expect(within(workflows).getByText("Which label applies?")).toBeInTheDocument();
    const pairs = screen.getByRole("region", { name: "Memory pairs" });
    expect(within(pairs).getByText("same email address")).toBeInTheDocument();
    const suggestions = screen.getByRole("region", { name: "Skill suggestions" });
    expect(within(suggestions).getByText("clearer steps")).toBeInTheDocument();
    const imports = screen.getByRole("region", { name: "Skill imports" });
    expect(within(imports).getByText("imported")).toBeInTheDocument();
  });

  it("decides an approval through the REST route, then refreshes and reports the count", async () => {
    const user = userEvent.setup();
    const onCountChange = vi.fn();
    vi.mocked(api.listPending)
      .mockResolvedValueOnce(list([APPROVAL, SUGGESTION]))
      .mockResolvedValueOnce(list([SUGGESTION]));
    vi.mocked(api.decideApproval).mockResolvedValue({
      status: "applied",
      message: "Done.",
      approval_id: APPROVAL.id,
    });

    render(wrap(<PendingView onCountChange={onCountChange} />));
    await user.click(await screen.findByRole("button", { name: "Approve" }));

    expect(api.decideApproval).toHaveBeenCalledWith("tok", APPROVAL.id, "approve");
    await waitFor(() => expect(api.listPending).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onCountChange).toHaveBeenLastCalledWith(1));
    expect(await screen.findByText("Approved and done.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Approvals" })).not.toBeInTheDocument();
  });

  it("shows a refused decision on its card and keeps the request", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValue(list([APPROVAL]));
    vi.mocked(api.decideApproval).mockRejectedValue(
      new api.ApiError(409, "HTTP 409", "installing dependencies needs a shell runner"),
    );

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "installing dependencies needs a shell runner",
    );
    expect(screen.getByRole("region", { name: "Approvals" })).toBeInTheDocument();
  });

  it("resumes a workflow run with the answers typed in its form", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending)
      .mockResolvedValueOnce(list([WORKFLOW]))
      .mockResolvedValueOnce(list([]));
    vi.mocked(api.runWorkflow).mockResolvedValue({} as api.WorkflowRunResult);

    render(wrap(<PendingView />));
    await user.type(await screen.findByPlaceholderText(/answer/i), "the billing label");
    await user.click(screen.getByRole("button", { name: "Resume run" }));

    expect(api.runWorkflow).toHaveBeenCalledWith(
      "tok", "triage", "the billing label", [], "", "", "wrun1",
    );
    expect(await screen.findByText("Nothing is waiting on you.")).toBeInTheDocument();
  });

  it("accepts a skill suggestion from its card", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending)
      .mockResolvedValueOnce(list([SUGGESTION]))
      .mockResolvedValueOnce(list([]));
    vi.mocked(api.acceptSkillSuggestion).mockResolvedValue({ ok: true });

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Accept" }));

    expect(api.acceptSkillSuggestion).toHaveBeenCalledWith("tok", "sug1");
    await waitFor(() => expect(api.listPending).toHaveBeenCalledTimes(2));
  });

  it("sends a skill import to its triage in Skills", async () => {
    const user = userEvent.setup();
    const onOpenSkill = vi.fn();
    vi.mocked(api.listPending).mockResolvedValue(list([QUARANTINE]));

    render(wrap(<PendingView onOpenSkill={onOpenSkill} />));
    await user.click(await screen.findByRole("button", { name: "Review in Skills" }));

    expect(onOpenSkill).toHaveBeenCalledWith("imported");
  });

  it("says when nothing waits, and names a source that failed to load", async () => {
    vi.mocked(api.listPending).mockResolvedValue(
      list([], [{ source: "flagged_pair", detail: "disk unreadable" }]),
    );

    render(wrap(<PendingView />));

    expect(await screen.findByText("Nothing is waiting on you.")).toBeInTheDocument();
    expect(screen.getByText("Could not load Memory pairs: disk unreadable")).toBeInTheDocument();
  });
});
