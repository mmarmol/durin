import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RunsView, strandedRuns } from "@/components/workflows/RunsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAllWorkflowRuns: vi.fn(),
    getWorkflowRunManifest: vi.fn(),
    runWorkflow: vi.fn(),
  };
});

beforeEach(() => {
  if (!navigator.clipboard) {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: () => Promise.resolve() },
      configurable: true,
    });
  }
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

const NEEDS_INPUT: api.WorkflowGlobalRun = {
  workflow: "onboarding",
  run_id: "run-waiting",
  status: "needs_input",
  started_at: 1000,
  finished_at: null,
  task: "set up the account",
  needs_input_node: "ask",
  questions: "Which environment — staging or prod?",
};

const COMPLETED: api.WorkflowGlobalRun = {
  workflow: "digest",
  run_id: "run-done",
  status: "completed",
  started_at: 2000,
  finished_at: 2100,
  task: "summarize the week",
  needs_input_node: null,
};

describe("strandedRuns", () => {
  it("filters to only needs_input entries", () => {
    expect(strandedRuns([NEEDS_INPUT, COMPLETED]).map((r) => r.run_id)).toEqual(["run-waiting"]);
  });

  it("is empty when nothing is waiting", () => {
    expect(strandedRuns([COMPLETED])).toEqual([]);
  });
});

describe("RunsView", () => {
  it("renders the tray with questions and posts a resume with {workflow, answers, run_id}", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, COMPLETED]);
    vi.mocked(api.runWorkflow).mockResolvedValue({
      status: "completed", final_output: "done", run_id: "run-waiting", runs: [],
    });
    const user = userEvent.setup();
    render(wrap(<RunsView />));

    await screen.findByText("set up the account");
    expect(screen.getByText(/Which environment — staging or prod\?/)).toBeInTheDocument();

    const textarea = screen.getByPlaceholderText(/Type your answers/i);
    await user.type(textarea, "prod");
    await user.click(screen.getByRole("button", { name: /Resume run/i }));

    await waitFor(() =>
      expect(api.runWorkflow).toHaveBeenCalledWith("tok", "onboarding", "prod", [], "", "", "run-waiting"),
    );
  });

  it("renders the feed with status chips for every run", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, COMPLETED]);
    render(wrap(<RunsView />));

    await screen.findByText("summarize the week");
    expect(screen.getByText("set up the account")).toBeInTheDocument();
  });

  it("filters the feed by status", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, COMPLETED]);
    const user = userEvent.setup();
    render(wrap(<RunsView />));

    await screen.findByText("summarize the week");
    await user.selectOptions(screen.getByLabelText(/Filter by status/i), "completed");
    expect(screen.getByText("summarize the week")).toBeInTheDocument();
    // The needs_input row's task text should no longer be shown once filtered out.
    expect(screen.queryByText("set up the account")).not.toBeInTheDocument();
  });

  it("filters the feed by workflow", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([NEEDS_INPUT, COMPLETED]);
    const user = userEvent.setup();
    render(wrap(<RunsView />));

    await screen.findByText("summarize the week");
    await user.selectOptions(screen.getByLabelText(/Filter by workflow/i), "digest");
    expect(screen.getByText("summarize the week")).toBeInTheDocument();
    expect(screen.queryByText("set up the account")).not.toBeInTheDocument();
  });

  it("fetches and shows a run's manifest detail when a feed entry is clicked", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([COMPLETED]);
    vi.mocked(api.getWorkflowRunManifest).mockResolvedValue({
      status: "completed",
      final_output: "the weekly digest",
      run_id: "run-done",
      runs: [],
    });
    const user = userEvent.setup();
    render(wrap(<RunsView />));

    await screen.findByText("summarize the week");
    await user.click(screen.getByText("summarize the week"));

    expect(await screen.findByText("the weekly digest")).toBeInTheDocument();
    expect(api.getWorkflowRunManifest).toHaveBeenCalledWith("tok", "digest", "run-done");
  });

  it("marks a sub-run whose parent is not in the list with a 'sub of' marker", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([
      { ...COMPLETED, parent_run_id: "orphan-parent" },
    ]);
    render(wrap(<RunsView />));
    await screen.findByText("summarize the week");
    expect(screen.getByText(/sub of orphan-parent/i)).toBeInTheDocument();
  });

  it("shows an empty state when there are no runs", async () => {
    vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([]);
    render(wrap(<RunsView />));
    expect(await screen.findByText(/No runs match this filter/i)).toBeInTheDocument();
  });
});
