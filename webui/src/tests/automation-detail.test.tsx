import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AutomationsView } from "@/components/AutomationsView";
import { LiveRunCard } from "@/components/automations/LiveRunCard";
import { outcomeChipCls, RunHistory } from "@/components/automations/RunHistory";
import { RunDetailCard } from "@/components/automations/RunDetailCard";
import * as api from "@/lib/api";
import type { InboundEvent } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";
import type { DurinClient } from "@/lib/durin-client";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAutomations: vi.fn(),
    listAllAutomationRuns: vi.fn(),
    listAutomationRuns: vi.fn(),
    listCronJobs: vi.fn(),
    listWorkflows: vi.fn(),
    getWorkflowRunManifest: vi.fn(),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listAutomations).mockResolvedValue([]);
  vi.mocked(api.listAllAutomationRuns).mockResolvedValue([]);
  vi.mocked(api.listAutomationRuns).mockResolvedValue([]);
  vi.mocked(api.listCronJobs).mockResolvedValue([]);
  vi.mocked(api.listWorkflows).mockResolvedValue([]);
  vi.mocked(api.getWorkflowRunManifest).mockResolvedValue({
    status: "completed",
    final_output: "",
    run_id: "wr-1",
    runs: [],
  });
});
afterEach(() => vi.restoreAllMocks());

// A fake client whose onChat actually captures + replays, like durin-client's
// real chatHandlers map — narrow (single handler per chat_id, no fan-out,
// no reconnect) but enough for these components' own subscribe/filter logic.
function makeFakeClient() {
  const handlers = new Map<string, (ev: InboundEvent) => void>();
  const client = {
    onChat: vi.fn((chatId: string, handler: (ev: InboundEvent) => void) => {
      handlers.set(chatId, handler);
      return () => handlers.delete(chatId);
    }),
  };
  const emit = (chatId: string, ev: InboundEvent) => handlers.get(chatId)?.(ev);
  return { client, emit };
}

function wrap(children: ReactNode, client?: unknown) {
  return (
    <ClientProvider client={(client ?? {}) as unknown as DurinClient} token="tok">
      {children}
    </ClientProvider>
  );
}

function run(overrides: Partial<api.AutomationRun>): api.AutomationRun {
  return {
    automation: "soporte-guard",
    run_id: "r1",
    status: "completed",
    cause: { kind: "channel", excerpt: "Ticket #23124 · outbound mail stuck in queue" },
    started_at: 1_000,
    ...overrides,
  };
}

const AUTOMATION: api.AutomationSummary = {
  name: "soporte-guard",
  workflow: "slack-ticket-pipeline",
  enabled: true,
  triggers: [{ source: "channel", channel: "slack", filters: { chat: "C0GUARD" } }],
  delivery: { notify: "when_notable", silent_labels: [] },
  help: {},
  concurrency: "single",
  active_runs: 1,
  paused: 0,
  pending_events: 0,
  attempts: 0,
  achieved: false,
  stuck: false,
};

// -- RunHistory ---------------------------------------------------------

describe("RunHistory", () => {
  it("shows the outcome chip, cause excerpt and duration for a finished run", async () => {
    const finished = run({
      run_id: "r1",
      status: "completed",
      started_at: 1_000,
      finished_at: 1_372, // 372s → 6:12
      cause: { kind: "channel", excerpt: "Ticket #23124" },
    });
    render(wrap(<RunHistory runs={[finished]} selectedRunId={null} onSelect={() => {}} />));

    expect(await screen.findByText("completed")).toBeInTheDocument();
    expect(screen.getByText(/💬 Ticket #23124 · 6:12/)).toBeInTheDocument();
  });

  it("renders a delivered run's delivery line", () => {
    const delivered = run({
      run_id: "r2",
      delivery: { channel: "#guard-support", to: "thread", result: "delivered", at_ms: 1_000 },
    });
    render(wrap(<RunHistory runs={[delivered]} selectedRunId={null} onSelect={() => {}} />));
    expect(screen.getByText("→ #guard-support · delivered")).toBeInTheDocument();
  });

  it("renders 'silenced by policy' for a silenced delivery result", () => {
    const silenced = run({
      run_id: "r3",
      delivery: { channel: "#guard-support", to: "", result: "silenced", at_ms: 1_000 },
    });
    render(wrap(<RunHistory runs={[silenced]} selectedRunId={null} onSelect={() => {}} />));
    expect(screen.getByText("→ #guard-support · silenced by policy")).toBeInTheDocument();
  });

  it("renders no delivery line at all when delivery is null", () => {
    const noDelivery = run({ run_id: "r4", delivery: null });
    render(wrap(<RunHistory runs={[noDelivery]} selectedRunId={null} onSelect={() => {}} />));
    expect(screen.queryByText(/→/)).not.toBeInTheDocument();
  });

  it("sorts newest-first regardless of input order", () => {
    const older = run({ run_id: "old", started_at: 1_000, cause: { kind: "manual", excerpt: "older cause" } });
    const newer = run({ run_id: "new", started_at: 5_000, cause: { kind: "manual", excerpt: "newer cause" } });
    render(wrap(<RunHistory runs={[older, newer]} selectedRunId={null} onSelect={() => {}} />));
    const rows = screen.getAllByTestId("run-history-row");
    expect(rows[0]).toHaveTextContent("newer cause");
    expect(rows[1]).toHaveTextContent("older cause");
  });

  it("reports the clicked run and marks the selected one aria-current", async () => {
    const a = run({ run_id: "a" });
    const b = run({ run_id: "b", started_at: 500 });
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(wrap(<RunHistory runs={[a, b]} selectedRunId="a" onSelect={onSelect} />));

    const rows = screen.getAllByTestId("run-history-row");
    expect(rows[0]).toHaveAttribute("aria-current", "true");
    expect(rows[1]).not.toHaveAttribute("aria-current");

    await user.click(rows[1]);
    expect(onSelect).toHaveBeenCalledWith(b);
  });

  it("shows the empty state when there are no runs", () => {
    render(wrap(<RunHistory runs={[]} selectedRunId={null} onSelect={() => {}} />));
    expect(screen.getByText(/no runs yet/i)).toBeInTheDocument();
  });
});

describe("outcomeChipCls", () => {
  it("mirrors RunDots' tone mapping (the C2 color language)", () => {
    expect(outcomeChipCls("achieved")).toContain("primary");
    expect(outcomeChipCls("completed")).toContain("primary");
    expect(outcomeChipCls("failed")).toContain("destructive");
    expect(outcomeChipCls("rejected")).toContain("warn");
    expect(outcomeChipCls("interrupted")).toContain("warn");
    expect(outcomeChipCls("running")).toContain("accent");
    expect(outcomeChipCls("paused")).toContain("accent");
  });
});

// -- RunDetailCard --------------------------------------------------------

describe("RunDetailCard", () => {
  it("renders the quoted cause, outcome chip with duration, delivery and approval records", async () => {
    const r = run({
      run_id: "a3f2c9",
      status: "completed",
      started_at: 1_000,
      finished_at: 1_372,
      cause: { kind: "channel", excerpt: "Ticket #23124 · «Correo saliente atascado en cola»" },
      delivery: { channel: "#guard-support", to: "thread", result: "delivered", at_ms: 1_000 },
      approval: { action: "approve", by: "@marcelo", at_ms: 2_000 },
      workflow_run_id: null,
    });
    render(wrap(<RunDetailCard run={r} automation={AUTOMATION} onOpenWorkflowRun={() => {}} />));

    expect(screen.getByText(/Ticket #23124 · «Correo saliente atascado en cola»/)).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("6:12")).toBeInTheDocument();
    expect(screen.getByText(/→ #guard-support · thread/)).toBeInTheDocument();
    expect(screen.getByText(/delivered/)).toBeInTheDocument();
    expect(screen.getByText(/approved by @marcelo/)).toBeInTheDocument();
  });

  it("omits the delivery and approval rows entirely when null", () => {
    const r = run({ delivery: null, approval: null, workflow_run_id: null });
    render(wrap(<RunDetailCard run={r} automation={AUTOMATION} onOpenWorkflowRun={() => {}} />));
    expect(screen.queryByText("Delivery")).not.toBeInTheDocument();
    expect(screen.queryByText("Approval")).not.toBeInTheDocument();
  });

  it("fetches the workflow manifest and renders its final output and files", async () => {
    vi.mocked(api.getWorkflowRunManifest).mockResolvedValue({
      status: "completed",
      final_output: "Root cause: expired cert. Rotation suggested.",
      run_id: "wr-1",
      runs: [],
      output_files: ["evidencia.md", "pipeline-complete.json"],
      output_dir: "keys/ticket-23124/",
    });
    const r = run({ workflow_run_id: "wr-1" });
    render(wrap(<RunDetailCard run={r} automation={AUTOMATION} onOpenWorkflowRun={() => {}} />));

    expect(await screen.findByText(/Root cause: expired cert/)).toBeInTheDocument();
    expect(api.getWorkflowRunManifest).toHaveBeenCalledWith("tok", "slack-ticket-pipeline", "wr-1");
    expect(screen.getByText("evidencia.md")).toBeInTheDocument();
    expect(screen.getByText("pipeline-complete.json")).toBeInTheDocument();
    expect(screen.getByText(/keys\/ticket-23124\//)).toBeInTheDocument();
  });

  it("never fetches a manifest, and hides the drill-in link, when workflow_run_id is null", () => {
    const r = run({ workflow_run_id: null });
    render(wrap(<RunDetailCard run={r} automation={AUTOMATION} onOpenWorkflowRun={() => {}} />));
    expect(api.getWorkflowRunManifest).not.toHaveBeenCalled();
    expect(screen.queryByText(/ver ejecución completa|view full execution/i)).not.toBeInTheDocument();
  });

  it("the drill-in link calls onOpenWorkflowRun with the automation's workflow and the run's workflow_run_id", async () => {
    const onOpenWorkflowRun = vi.fn();
    const r = run({ workflow_run_id: "wr-42" });
    const user = userEvent.setup();
    render(wrap(<RunDetailCard run={r} automation={AUTOMATION} onOpenWorkflowRun={onOpenWorkflowRun} />));

    await user.click(await screen.findByRole("button", { name: /ver ejecución completa|view full execution/i }));
    expect(onOpenWorkflowRun).toHaveBeenCalledWith("slack-ticket-pipeline", "wr-42");
  });

  it("phrases a reject and a revise approval action distinctly", () => {
    const rejected = run({ approval: { action: "reject", by: "@marcelo", at_ms: 1000 }, workflow_run_id: null });
    const { rerender } = render(wrap(<RunDetailCard run={rejected} automation={AUTOMATION} onOpenWorkflowRun={() => {}} />));
    expect(screen.getByText(/rejected by @marcelo/)).toBeInTheDocument();

    const revised = run({ approval: { action: "revise", by: "@marcelo", at_ms: 1000 }, workflow_run_id: null });
    rerender(wrap(<RunDetailCard run={revised} automation={AUTOMATION} onOpenWorkflowRun={() => {}} />));
    expect(screen.getByText(/revised by @marcelo/)).toBeInTheDocument();
  });
});

// -- LiveRunCard ------------------------------------------------------------

describe("LiveRunCard", () => {
  function workflowProgressFrame(
    runId: string,
    nodes: Array<{ id: string; status: "running" | "done" | "failed" | "pending" }>,
  ): InboundEvent {
    return {
      event: "message",
      chat_id: "runs:feed",
      text: "",
      kind: "progress",
      tool_events: [
        {
          call_id: `workflow:${runId}`,
          name: "workflow_progress",
          phase: "running",
          arguments: { workflow: "slack-ticket-pipeline" },
          nodes,
        },
      ],
    };
  }

  it("renders the cause line", () => {
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: null, cause: { kind: "channel", excerpt: "Ticket #23124" } });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" />, client));
    expect(screen.getByText(/💬 Ticket #23124/)).toBeInTheDocument();
  });

  it("reduces a live workflow_progress frame into done / running / pending node rows", async () => {
    const { client, emit } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: "wr-1" });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" />, client));
    // Flush the mount-time manifest poll (workflow_run_id is set, so it fires
    // regardless) before asserting, so its state update can't land outside
    // an act() boundary and flake a later test's console output.
    await act(async () => {});

    act(() => {
      emit(
        "runs:feed",
        workflowProgressFrame("wr-1", [
          { id: "resolver-contexto", status: "done" },
          { id: "redactar-nota", status: "running" },
          { id: "publicar-zendesk", status: "pending" },
        ]),
      );
    });

    const card = screen.getByTestId("live-run-card");
    expect(within(card).getByText("resolver-contexto")).toBeInTheDocument();
    expect(within(card).getByText("redactar-nota")).toBeInTheDocument();
    expect(within(card).getByText("publicar-zendesk")).toBeInTheDocument();
    expect(within(card).getByText("○")).toBeInTheDocument();
  });

  it("ignores a frame for a different run's call_id", async () => {
    const { client, emit } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: "wr-1" });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" />, client));
    await act(async () => {});

    act(() => {
      emit("runs:feed", workflowProgressFrame("some-other-run", [{ id: "other-node", status: "running" }]));
    });

    expect(screen.queryByText("other-node")).not.toBeInTheDocument();
  });

  it("never subscribes or polls when workflow_run_id is null", () => {
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: null });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" />, client));
    expect(client.onChat).not.toHaveBeenCalled();
    expect(api.getWorkflowRunManifest).not.toHaveBeenCalled();
  });

  it("falls back to the polled manifest's nodes while no live frame has arrived", async () => {
    vi.mocked(api.getWorkflowRunManifest).mockResolvedValue({
      status: "running",
      final_output: "",
      run_id: "wr-1",
      runs: [
        { node_id: "resolver-contexto", iteration: 1, passed: null, session_key: null, worker_index: null, status: "ok", route_label: null, duration_s: 62 },
      ],
      active_node: { node_id: "redactar-nota", label: "redactar-nota", started_at: 900 },
    });
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: "wr-1" });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" />, client));

    expect(await screen.findByText("resolver-contexto")).toBeInTheDocument();
    expect(screen.getByText("redactar-nota")).toBeInTheDocument();
    expect(api.getWorkflowRunManifest).toHaveBeenCalledWith("tok", "slack-ticket-pipeline", "wr-1");
  });
});

// -- List → DetailView wiring (via AutomationsView) --------------------------

describe("AutomationsView → DetailView", () => {
  it("clicking a row opens the detail view; back returns to the list", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue([AUTOMATION]);
    vi.mocked(api.listAutomationRuns).mockResolvedValue([]);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />, {}));

    await user.click(await screen.findByTestId("automation-row-open"));

    expect(await screen.findByText("Runs")).toBeInTheDocument(); // RunHistory's own cardhead
    expect(screen.queryByText(/no automations yet/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /back/i }));
    expect(await screen.findByText("soporte-guard")).toBeInTheDocument();
  });

  it("shows a LiveRunCard for each of this automation's running runs", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue([AUTOMATION]);
    vi.mocked(api.listAutomationRuns).mockResolvedValue([
      run({ run_id: "running-1", status: "running", cause: { kind: "manual", excerpt: "run now" } }),
      run({ run_id: "done-1", status: "completed" }),
    ]);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />, {}));

    await user.click(await screen.findByTestId("automation-row-open"));

    await waitFor(() => expect(screen.getAllByTestId("live-run-card")).toHaveLength(1));
  });

  it("passes (workflow, runId) up to onOpenWorkflowRun from a selected run's drill-in", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue([AUTOMATION]);
    vi.mocked(api.listAutomationRuns).mockResolvedValue([run({ run_id: "r9", workflow_run_id: "wr-9" })]);
    const onOpenWorkflowRun = vi.fn();
    const user = userEvent.setup();
    render(wrap(<AutomationsView onOpenWorkflowRun={onOpenWorkflowRun} />, {}));

    await user.click(await screen.findByTestId("automation-row-open"));
    await user.click(await screen.findByTestId("run-history-row"));
    await user.click(await screen.findByRole("button", { name: /ver ejecución completa|view full execution/i }));

    expect(onOpenWorkflowRun).toHaveBeenCalledWith("slack-ticket-pipeline", "wr-9");
  });
});
