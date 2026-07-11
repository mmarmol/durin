import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowsView } from "@/components/WorkflowsView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listWorkflows: vi.fn(),
    listPersonas: vi.fn(),
    getWorkflow: vi.fn(),
    getWorkflowRecommendations: vi.fn(),
    applyWorkflowRecommendation: vi.fn(),
    dismissWorkflowRecommendation: vi.fn(),
    listWorkflowRuns: vi.fn(),
    listAllWorkflowRuns: vi.fn(),
  };
});

beforeEach(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  vi.mocked(api.listWorkflows).mockResolvedValue(["demo"]);
  vi.mocked(api.listPersonas).mockResolvedValue({ personas: [], default: null });
  vi.mocked(api.getWorkflow).mockResolvedValue({
    name: "demo",
    start: "start",
    nodes: [{ id: "start", kind: "work", mode: "build", prompt: "", next: null }],
  } as never);
  vi.mocked(api.applyWorkflowRecommendation).mockResolvedValue({ ok: true, detail: "" });
  vi.mocked(api.dismissWorkflowRecommendation).mockResolvedValue({ ok: true, detail: "" });
  vi.mocked(api.listWorkflowRuns).mockResolvedValue([]);
  vi.mocked(api.listAllWorkflowRuns).mockResolvedValue([]);
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

describe("WorkflowsView recommendations banner", () => {
  it("renders a command recommendation with a current->proposed presentation and a manual-only badge", async () => {
    vi.mocked(api.getWorkflowRecommendations).mockResolvedValue([
      {
        id: "rec-command",
        target_id: "gatekeeper",
        field: "command",
        current: "python check.py --old",
        proposed: "python check.py --new",
        reason: "recurring node_failed with a stale flag",
        status: "open",
        count: 3,
        manual_only: true,
      },
    ]);
    render(wrap(<WorkflowsView />));

    await screen.findByText("gatekeeper");
    expect(screen.getByText("command")).toBeInTheDocument();
    expect(screen.getByText("python check.py --old")).toBeInTheDocument();
    expect(screen.getByText("python check.py --new")).toBeInTheDocument();
    expect(screen.getByText("gate edit — needs your review")).toBeInTheDocument();
    // Never a "prompt" label on a command recommendation.
    expect(screen.queryByText("prompt")).not.toBeInTheDocument();
  });

  it("renders a script_file recommendation with the filename and content presentation", async () => {
    vi.mocked(api.getWorkflowRecommendations).mockResolvedValue([
      {
        id: "rec-script",
        kind: "script_file",
        script: "validate.sh",
        current: "#!/bin/sh\nexit 1",
        proposed: "#!/bin/sh\nexit 0",
        reason: "fixed the broken exit path",
        status: "open",
        count: 1,
      },
    ]);
    render(wrap(<WorkflowsView />));

    await screen.findByText("validate.sh");
    expect(screen.getByText("script file")).toBeInTheDocument();
    expect(screen.getByText(/exit 1/)).toBeInTheDocument();
    expect(screen.getByText(/exit 0/)).toBeInTheDocument();
  });

  it("still renders structural suggestions without regression", async () => {
    vi.mocked(api.getWorkflowRecommendations).mockResolvedValue([
      {
        id: "rec-structural",
        kind: "structural",
        reason: "consider splitting this node",
        why_rejected: "outside prompt-only scope",
        diagnostic: "3 failures across 5 runs",
        status: "open",
        count: 1,
      },
    ]);
    render(wrap(<WorkflowsView />));

    await screen.findByText("consider splitting this node");
    expect(screen.getByText(/3 failures across 5 runs/)).toBeInTheDocument();
  });

  it("applies a script_file recommendation via the same apply endpoint", async () => {
    vi.mocked(api.getWorkflowRecommendations).mockResolvedValue([
      {
        id: "rec-script",
        kind: "script_file",
        script: "validate.sh",
        current: "old",
        proposed: "new",
        reason: "fix",
        status: "open",
        count: 1,
      },
    ]);
    const user = userEvent.setup();
    render(wrap(<WorkflowsView />));

    await screen.findByText("validate.sh");
    await user.click(screen.getByRole("button", { name: /apply/i }));
    expect(api.applyWorkflowRecommendation).toHaveBeenCalledWith("tok", "demo", "rec-script");
  });
});
