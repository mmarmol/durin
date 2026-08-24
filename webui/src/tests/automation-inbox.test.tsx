import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AutomationsView } from "@/components/AutomationsView";
import { InboxView } from "@/components/automations/InboxView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAutomations: vi.fn(),
    listAllAutomationRuns: vi.fn(),
    listCronJobs: vi.fn(),
    answerAutomationRun: vi.fn(),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listAutomations).mockResolvedValue([]);
  vi.mocked(api.listAllAutomationRuns).mockResolvedValue([]);
  vi.mocked(api.listCronJobs).mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

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

function run(overrides: Partial<api.AutomationRun>): api.AutomationRun {
  return {
    automation: "cobrar-fac-1042",
    run_id: "r-fac-a",
    status: "paused",
    cause: { kind: "schedule", excerpt: "" },
    started_at: 1_000,
    ...overrides,
  };
}

const APPROVAL: api.AutomationRun = run({
  ask_kind: "approval",
  ask: "Send a reminder to client@acme.com about invoice FAC-1042, due 08/12",
  proposal: "Subject: Invoice FAC-1042 reminder…",
});

const QUESTION: api.AutomationRun = run({
  run_id: "r-guard-q",
  automation: "soporte-guard",
  ask_kind: "question",
  ask: "Which environment, EU or US?",
  proposal: null,
});

// -- InboxView (direct) ------------------------------------------------------

describe("InboxView", () => {
  it("renders the automation name, capped ask text and the quoted proposal for an approval", () => {
    render(wrap(<InboxView run={APPROVAL} onResolved={() => {}} />));

    expect(screen.getByText("cobrar-fac-1042")).toBeInTheDocument();
    expect(screen.getByText(/Send a reminder to client@acme.com/)).toBeInTheDocument();
    expect(screen.getByText("Subject: Invoice FAC-1042 reminder…")).toBeInTheDocument();
  });

  it("Aprobar posts action 'approve' with empty text and reports resolution", async () => {
    vi.mocked(api.answerAutomationRun).mockResolvedValue({ ...APPROVAL, status: "completed" });
    const onResolved = vi.fn();
    const user = userEvent.setup();
    render(wrap(<InboxView run={APPROVAL} onResolved={onResolved} />));

    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(api.answerAutomationRun).toHaveBeenCalledWith("tok", "cobrar-fac-1042", "r-fac-a", "", "approve"),
    );
    expect(onResolved).toHaveBeenCalledTimes(1);
    expect(onResolved).toHaveBeenCalledWith(expect.any(String));
  });

  it("Corregir stays disabled until a comment is typed, then posts action 'revise' with that comment", async () => {
    vi.mocked(api.answerAutomationRun).mockResolvedValue({ ...APPROVAL, status: "completed" });
    const user = userEvent.setup();
    render(wrap(<InboxView run={APPROVAL} onResolved={() => {}} />));

    const reviseButton = screen.getByRole("button", { name: "Revise" });
    expect(reviseButton).toBeDisabled();

    await user.type(
      screen.getByPlaceholderText(/What to fix/i),
      "drop the amount from the subject",
    );
    expect(reviseButton).toBeEnabled();
    await user.click(reviseButton);

    await waitFor(() =>
      expect(api.answerAutomationRun).toHaveBeenCalledWith(
        "tok",
        "cobrar-fac-1042",
        "r-fac-a",
        "drop the amount from the subject",
        "revise",
      ),
    );
  });

  it("a whitespace-only comment does not enable Corregir", async () => {
    const user = userEvent.setup();
    render(wrap(<InboxView run={APPROVAL} onResolved={() => {}} />));

    await user.type(screen.getByPlaceholderText(/What to fix/i), "   ");
    expect(screen.getByRole("button", { name: "Revise" })).toBeDisabled();
  });

  it("Rechazar posts action 'reject'", async () => {
    vi.mocked(api.answerAutomationRun).mockResolvedValue({ ...APPROVAL, status: "rejected" });
    const user = userEvent.setup();
    render(wrap(<InboxView run={APPROVAL} onResolved={() => {}} />));

    await user.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() =>
      expect(api.answerAutomationRun).toHaveBeenCalledWith("tok", "cobrar-fac-1042", "r-fac-a", "", "reject"),
    );
  });

  it("renders a question's full text and Responder y reanudar posts the typed answer with no action", async () => {
    vi.mocked(api.answerAutomationRun).mockResolvedValue({ ...QUESTION, status: "completed" });
    const onResolved = vi.fn();
    const user = userEvent.setup();
    render(wrap(<InboxView run={QUESTION} onResolved={onResolved} />));

    expect(screen.getByText("Which environment, EU or US?")).toBeInTheDocument();

    const answerButton = screen.getByRole("button", { name: /Answer & resume/i });
    expect(answerButton).toBeDisabled();

    await user.type(screen.getByPlaceholderText(/Your answer/i), "Guard EU");
    expect(answerButton).toBeEnabled();
    await user.click(answerButton);

    await waitFor(() =>
      expect(api.answerAutomationRun).toHaveBeenCalledWith("tok", "soporte-guard", "r-guard-q", "Guard EU"),
    );
    // Exactly 4 positional args — answerAutomationRun's 5th (action) param is
    // genuinely omitted, not passed as undefined explicitly.
    const call = vi.mocked(api.answerAutomationRun).mock.calls[0];
    expect(call).toHaveLength(4);
    expect(onResolved).toHaveBeenCalledTimes(1);
  });

  it("renders an API error inline instead of throwing, and leaves the card usable to retry", async () => {
    vi.mocked(api.answerAutomationRun).mockRejectedValueOnce(new Error("network down"));
    const onResolved = vi.fn();
    const user = userEvent.setup();
    render(wrap(<InboxView run={APPROVAL} onResolved={onResolved} />));

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("network down")).toBeInTheDocument();
    expect(onResolved).not.toHaveBeenCalled();
    // The card is still interactive — the button isn't stuck disabled after the failure.
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });
});

// -- AutomationsView -> InboxView wiring -------------------------------------

describe("AutomationsView -> InboxView wiring", () => {
  it("Revisar expands the approval inline; resolving it refreshes the feed and the card leaves", async () => {
    vi.mocked(api.listAllAutomationRuns).mockResolvedValueOnce([APPROVAL]).mockResolvedValue([]);
    vi.mocked(api.answerAutomationRun).mockResolvedValue({ ...APPROVAL, status: "completed" });
    const user = userEvent.setup();
    render(wrap(<AutomationsView />));

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect(await screen.findByText("Subject: Invoice FAC-1042 reminder…")).toBeInTheDocument();

    const callsBefore = vi.mocked(api.listAllAutomationRuns).mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(vi.mocked(api.listAllAutomationRuns).mock.calls.length).toBeGreaterThan(callsBefore),
    );
    await waitFor(() =>
      expect(screen.queryByText("Subject: Invoice FAC-1042 reminder…")).not.toBeInTheDocument(),
    );
    // The tray itself is also gone now — the run is no longer paused in the
    // (mocked) refreshed feed, and the tray hides entirely when nothing is
    // pending, so nothing here still says "Needs you".
    expect(screen.queryByText("Needs you")).not.toBeInTheDocument();
  });

  it("Responder on a question expands its own card, distinct from an approval's", async () => {
    vi.mocked(api.listAllAutomationRuns).mockResolvedValue([QUESTION]);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />));

    await user.click(await screen.findByRole("button", { name: "Answer" }));
    expect(await screen.findByText("Which environment, EU or US?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Answer & resume/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
