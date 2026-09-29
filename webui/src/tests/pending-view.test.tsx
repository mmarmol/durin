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
    cancelWorkflowRun: vi.fn(),
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

  it("cancels a workflow run only after confirming in the page", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending)
      .mockResolvedValueOnce(list([WORKFLOW]))
      .mockResolvedValueOnce(list([]));
    vi.mocked(api.cancelWorkflowRun).mockResolvedValue({ run_id: "wrun1", status: "cancelled" });

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    expect(api.cancelWorkflowRun).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel run" }));

    expect(api.cancelWorkflowRun).toHaveBeenCalledWith("tok", "triage", "wrun1");
    await waitFor(() => expect(api.listPending).toHaveBeenCalledTimes(2));
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

  it("scrolls to the section it was opened for", async () => {
    const scrolled: string[] = [];
    vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(function (this: Element) {
      scrolled.push(this.getAttribute("aria-label") ?? "");
    });
    vi.mocked(api.listPending).mockResolvedValue(list([APPROVAL, WORKFLOW, PAIR, SUGGESTION]));

    render(wrap(<PendingView focusSource="flagged_pair" />));
    await screen.findByRole("region", { name: "Memory pairs" });

    await waitFor(() => expect(scrolled).toEqual(["Memory pairs"]));
  });
});

// The dream's decisions are decided here, with the same cards the Dream page
// used to show: these carry the card behavior that lived in its inbox tab.
describe("PendingView — the dream's memory pairs and skill suggestions", () => {
  const basePair = {
    ref_a: "person:alice",
    ref_b: "person:alice-smith",
    verdict: "same_entity",
    confidence: 72,
    reasoning: "Both refs share the same name and email.",
    at_ms: 1_000,
    name_a: "Alice",
    name_b: "Alice Smith",
    aliases_a: ["Alice"],
    aliases_b: ["Alice", "A. Smith"],
    proposal: null,
    source: null,
  };
  const pairItem = (data: Record<string, unknown> = basePair) =>
    item("flagged_pair", `${data.ref_a}|${data.ref_b}`, data);

  it("merges a pair and drops it once the list refreshes", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending)
      .mockResolvedValueOnce(list([pairItem()]))
      .mockResolvedValue(list([]));
    vi.mocked(api.resolveFlaggedPair).mockResolvedValue({ ok: true, action: "merge" });

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Merge into alice-smith" }));

    expect(api.resolveFlaggedPair).toHaveBeenCalledWith("tok", {
      ref_a: "person:alice",
      ref_b: "person:alice-smith",
      action: "merge",
      survivor: "person:alice-smith",
    });
    await waitFor(() => expect(screen.queryByText("person:alice")).not.toBeInTheDocument());
  });

  it("keeps a pair separate", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValueOnce(list([pairItem()])).mockResolvedValue(list([]));
    vi.mocked(api.resolveFlaggedPair).mockResolvedValue({ ok: true, action: "separate" });

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Keep separate" }));

    expect(api.resolveFlaggedPair).toHaveBeenCalledWith("tok", {
      ref_a: "person:alice", ref_b: "person:alice-smith", action: "separate",
    });
  });

  it("shows the error on a pair that could not be resolved, and keeps it", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValue(list([pairItem()]));
    vi.mocked(api.resolveFlaggedPair).mockRejectedValue(new Error("HTTP 409"));

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Merge into alice-smith" }));

    expect(await screen.findByText(/Could not resolve pair/)).toBeInTheDocument();
    expect(screen.getByText("person:alice")).toBeInTheDocument();
  });

  it("shows the judge's proposal and applies it", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValueOnce(list([pairItem({
      ...basePair,
      verdict: "related",
      source: "tier2",
      proposal: {
        kind: "relate",
        renames: { "person:alice": { slug: "alice-jones", name: null } },
        alias_moves: [{ alias: "Alice", keep_on: "person:alice" }],
        relation: { from_ref: "person:alice-smith", type: "married_name_of", to_ref: "person:alice" },
      },
    })])).mockResolvedValue(list([]));
    vi.mocked(api.resolveFlaggedPair).mockResolvedValue({ ok: true, action: "accept" });

    render(wrap(<PendingView />));

    expect(await screen.findByText("Rename person:alice → person:alice-jones")).toBeInTheDocument();
    expect(screen.getByText("Alias “Alice” → only person:alice")).toBeInTheDocument();
    expect(screen.getByText("person:alice-smith —married_name_of→ person:alice")).toBeInTheDocument();
    expect(screen.getByText("investigated")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply proposal" }));

    expect(api.resolveFlaggedPair).toHaveBeenCalledWith("tok", {
      ref_a: "person:alice", ref_b: "person:alice-smith", action: "accept",
    });
  });

  it("sends only what the user changed in the editor", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValueOnce(list([pairItem()])).mockResolvedValue(list([]));
    vi.mocked(api.resolveFlaggedPair).mockResolvedValue({ ok: true, action: "relate" });

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Edit…" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Alice" }), "b");
    const slug = screen.getByRole("textbox", { name: "Key person:alice" });
    await user.clear(slug);
    await user.type(slug, "alice-jones");
    await user.click(screen.getByRole("checkbox", { name: "Relate them" }));
    await user.type(screen.getByRole("textbox", { name: "Relation type" }), "sibling_of");
    await user.click(screen.getByRole("button", { name: "Apply changes" }));

    expect(api.resolveFlaggedPair).toHaveBeenCalledWith("tok", {
      ref_a: "person:alice",
      ref_b: "person:alice-smith",
      action: "relate",
      alias_moves: [{ alias: "Alice", keep_on: "person:alice-smith" }],
      renames: { "person:alice": { slug: "alice-jones" } },
      relation: { from_ref: "person:alice", type: "sibling_of", to_ref: "person:alice-smith" },
    });
  });

  it("shows the server's reason when an edit is rejected", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValue(list([pairItem()]));
    vi.mocked(api.resolveFlaggedPair).mockRejectedValue(
      new api.ApiError(422, "HTTP 422", "person:alice-jones already exists"),
    );

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Keep separate" }));

    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
  });

  it("shows the error on a suggestion that could not be applied, and keeps it", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValue(list([SUGGESTION]));
    vi.mocked(api.acceptSkillSuggestion).mockRejectedValue(new Error("409"));

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Accept" }));

    expect(await screen.findByText(/could not apply the suggestion/i)).toBeInTheDocument();
    expect(screen.getByText("clearer steps")).toBeInTheDocument();
  });

  it("shows the server's detail when a suggestion is refused", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValue(list([SUGGESTION]));
    vi.mocked(api.acceptSkillSuggestion).mockRejectedValue(
      new api.ApiError(409, "HTTP 409", "old text not found"),
    );

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Accept" }));

    expect(await screen.findByText("old text not found")).toBeInTheDocument();
  });

  it("explains a suggestion blocked by the import quarantine", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPending).mockResolvedValue(list([SUGGESTION]));
    vi.mocked(api.acceptSkillSuggestion).mockRejectedValue(
      new api.ApiError(409, "HTTP 409", "skill 'mailer' is awaiting review", {
        reason: "skill_quarantined",
        skill: "mailer",
      }),
    );

    render(wrap(<PendingView />));
    await user.click(await screen.findByRole("button", { name: "Accept" }));

    expect(await screen.findByText(/awaiting review in the import quarantine/i)).toBeInTheDocument();
  });
});
