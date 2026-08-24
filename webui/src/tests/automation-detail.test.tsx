import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AutomationsView } from "@/components/AutomationsView";
import { DetailView } from "@/components/automations/DetailView";
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
    fireAutomation: vi.fn(),
    stopAutomationRun: vi.fn(),
    saveAutomation: vi.fn(),
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
  vi.mocked(api.fireAutomation).mockResolvedValue({ run_id: "r-new" });
  vi.mocked(api.stopAutomationRun).mockResolvedValue(run({ status: "failed" }));
  vi.mocked(api.saveAutomation).mockResolvedValue(undefined);
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

  it("renders the correct icon for every cause.kind the backend actually produces", () => {
    // Locks the full vocabulary against durin/cli/commands.py's cron-tick
    // dispatch (source="schedule", not "cron" — a schedule-triggered run
    // rendered no icon at all before this test existed) and
    // durin/automations/matcher.py's _fire (source="webhook" for a
    // HookDispatcher-origin fire, now distinguished from an ordinary
    // "channel" fire instead of collapsing into it).
    const rows = [
      run({ run_id: "sched", cause: { kind: "schedule", excerpt: "cron tick" } }),
      run({ run_id: "hook", cause: { kind: "webhook", excerpt: "release payload" } }),
      run({ run_id: "chain", cause: { kind: "chain", excerpt: "upstream finished" } }),
      run({ run_id: "man", cause: { kind: "manual", excerpt: "run now" } }),
      run({ run_id: "cht", cause: { kind: "chat", excerpt: "asked in chat" } }),
    ];
    render(wrap(<RunHistory runs={rows} selectedRunId={null} onSelect={() => {}} />));

    expect(screen.getByText(/⏰ cron tick/)).toBeInTheDocument();
    expect(screen.getByText(/🪝 release payload/)).toBeInTheDocument();
    expect(screen.getByText(/⛓ upstream finished/)).toBeInTheDocument();
    expect(screen.getByText(/⚙ run now/)).toBeInTheDocument();
    expect(screen.getByText(/💬 asked in chat/)).toBeInTheDocument();
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

    // Bare findByText, no chained .toBeInTheDocument(): final_output renders
    // through MarkdownText's lazy Suspense boundary (see runs-view.test.tsx's
    // "renders the final output as markdown" fix note for the exact TOCTOU
    // mechanism a chained check on the same resolved reference risks here).
    await screen.findByText(/Root cause: expired cert/);
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
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));
    expect(screen.getByText(/💬 Ticket #23124/)).toBeInTheDocument();
  });

  it("reduces a live workflow_progress frame into done / running / pending node rows", async () => {
    const { client, emit } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: "wr-1" });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));
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
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));
    await act(async () => {});

    act(() => {
      emit("runs:feed", workflowProgressFrame("some-other-run", [{ id: "other-node", status: "running" }]));
    });

    expect(screen.queryByText("other-node")).not.toBeInTheDocument();
  });

  it("never subscribes or polls when workflow_run_id is null", () => {
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: null });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));
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
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));

    expect(await screen.findByText("resolver-contexto")).toBeInTheDocument();
    expect(screen.getByText("redactar-nota")).toBeInTheDocument();
    expect(api.getWorkflowRunManifest).toHaveBeenCalledWith("tok", "slack-ticket-pipeline", "wr-1");
  });

  it("shows the running node's elapsed-vs-typical comparison when the manifest has one for it", async () => {
    vi.mocked(api.getWorkflowRunManifest).mockResolvedValue({
      status: "running",
      final_output: "",
      run_id: "wr-1",
      runs: [],
      active_node: { node_id: "redactar-nota", label: "redactar-nota", started_at: Date.now() / 1000 },
      typical_s: { "redactar-nota": 90 },
    });
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: "wr-1" });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));

    // Elapsed is a live clock off Date.now() (unmocked here), so only the
    // static "typical 1:30" half is pinned exactly — the running node's own
    // mockup format is "{elapsed} / typical {typical}".
    expect(await screen.findByText(/\/ typical 1:30/)).toBeInTheDocument();
  });

  it("omits the typical comparison for a node the manifest has no typical_s entry for", async () => {
    vi.mocked(api.getWorkflowRunManifest).mockResolvedValue({
      status: "running",
      final_output: "",
      run_id: "wr-1",
      runs: [],
      active_node: { node_id: "brand-new-node", label: "brand-new-node", started_at: Date.now() / 1000 },
      // No typical_s at all — a workflow with no completed-run history yet.
    });
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: "wr-1" });
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));

    await screen.findByText("brand-new-node");
    expect(screen.queryByText(/typical/)).not.toBeInTheDocument();
  });

  it("Detener asks for confirmation; confirming calls stopAutomationRun with the automation-level run id and notifies the parent", async () => {
    const { client } = makeFakeClient();
    const r = run({ run_id: "r7", automation: "soporte-guard", status: "running", workflow_run_id: null });
    const onStopped = vi.fn();
    const user = userEvent.setup();
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={onStopped} />, client));

    await user.click(screen.getByRole("button", { name: /^detener$|^stop$/i }));
    expect(api.stopAutomationRun).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/r7/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /^detener$|^stop$/i }));

    await waitFor(() => expect(api.stopAutomationRun).toHaveBeenCalledWith("tok", "soporte-guard", "r7"));
    await waitFor(() => expect(onStopped).toHaveBeenCalled());
  });

  it("cancelling the stop confirmation does not call stopAutomationRun", async () => {
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: null });
    const user = userEvent.setup();
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));

    await user.click(screen.getByRole("button", { name: /^detener$|^stop$/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /cancel|cancelar/i }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(api.stopAutomationRun).not.toHaveBeenCalled();
  });

  it("renders a stopAutomationRun API error inline instead of throwing, and never calls onStopped", async () => {
    vi.mocked(api.stopAutomationRun).mockRejectedValueOnce(new Error("run already finished"));
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: null });
    const onStopped = vi.fn();
    const user = userEvent.setup();
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={onStopped} />, client));

    await user.click(screen.getByRole("button", { name: /^detener$|^stop$/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /^detener$|^stop$/i }));

    expect(await screen.findByText("run already finished")).toBeInTheDocument();
    expect(onStopped).not.toHaveBeenCalled();
  });

  it("after a graceful stop, offers Force stop instead of re-enabling Stop, and it calls stopAutomationRun with hard=true", async () => {
    const { client } = makeFakeClient();
    const r = run({ run_id: "r7", automation: "soporte-guard", status: "running", workflow_run_id: null });
    const user = userEvent.setup();
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={() => {}} />, client));

    await user.click(screen.getByRole("button", { name: /^detener$|^stop$/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /^detener$|^stop$/i }));
    await waitFor(() => expect(api.stopAutomationRun).toHaveBeenCalledTimes(1));

    // The graceful Stop button is gone; only Force stop remains.
    expect(screen.queryByRole("button", { name: /^detener$|^stop$/i })).not.toBeInTheDocument();
    const forceButton = screen.getByRole("button", { name: /forzar detención|force stop/i });

    await user.click(forceButton);

    await waitFor(() =>
      expect(api.stopAutomationRun).toHaveBeenNthCalledWith(2, "tok", "soporte-guard", "r7", true),
    );
  });

  it("a lost-race 422 from stop is treated as the run having moved on: no error banner, still calls onStopped", async () => {
    vi.mocked(api.stopAutomationRun).mockRejectedValueOnce(
      new api.ApiError(422, "HTTP 422", "run is not active"),
    );
    const { client } = makeFakeClient();
    const r = run({ status: "running", workflow_run_id: null });
    const onStopped = vi.fn();
    const user = userEvent.setup();
    render(wrap(<LiveRunCard run={r} workflow="slack-ticket-pipeline" onStopped={onStopped} />, client));

    await user.click(screen.getByRole("button", { name: /^detener$|^stop$/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /^detener$|^stop$/i }));

    await waitFor(() => expect(onStopped).toHaveBeenCalled());
    expect(screen.queryByText(/422/)).not.toBeInTheDocument();
    expect(screen.queryByText(/run is not active/)).not.toBeInTheDocument();
  });
});

// -- DetailView --------------------------------------------------------------

describe("DetailView", () => {
  it("derives the selected run from the live runs array, so a poll refresh updates the detail card in place instead of freezing a stale snapshot", async () => {
    vi.useFakeTimers();
    const listSpy = vi.mocked(api.listAutomationRuns);
    listSpy.mockClear();
    const runningSnapshot = run({
      run_id: "r1", status: "running", cause: { kind: "manual", excerpt: "run now" }, delivery: null,
    });
    const finishedSnapshot = run({
      run_id: "r1", status: "completed", cause: { kind: "manual", excerpt: "run now" },
      finished_at: 2_000,
      delivery: { channel: "#guard-support", to: "", result: "delivered", at_ms: 2_000 },
    });
    // First fetch (mount) returns the running snapshot; every fetch after
    // (the anyRunning poll, 4s later) returns the same run_id now finished —
    // simulating the run completing while its detail card is open.
    listSpy.mockResolvedValueOnce([runningSnapshot]).mockResolvedValue([finishedSnapshot]);

    render(wrap(<DetailView automation={AUTOMATION} onBack={() => {}} onOpenWorkflowRun={() => {}} onAutomationSaved={async () => {}} />, {}));
    await act(async () => {});

    // Plain getBy, not findBy: findBy's own internal polling relies on real
    // timers, which fake timers would stall — the mount fetch above is
    // already flushed by this point (RunsView's own poll tests use the same
    // pattern for the same reason).
    fireEvent.click(screen.getByTestId("run-history-row"));
    await act(async () => {});
    expect(within(screen.getByTestId("run-detail-card")).getByText("running")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });

    // Same run_id, same selection — but the object backing it is the fresh
    // one, not the one captured when the row was clicked.
    expect(within(screen.getByTestId("run-detail-card")).getByText("completed")).toBeInTheDocument();
    expect(within(screen.getByTestId("run-detail-card")).getByText(/→ #guard-support/)).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("polls while the parent feed shows activity, so a run started before this view had any runs of its own becomes visible", async () => {
    vi.useFakeTimers();
    const listSpy = vi.mocked(api.listAutomationRuns);
    listSpy.mockClear();
    // Mount fetch returns nothing (the detail was opened before the first
    // run existed); the fire itself blocks server-side, so only a poll can
    // surface the run — and anyRunning can't arm it from an empty list.
    listSpy
      .mockResolvedValueOnce([])
      .mockResolvedValue([run({ run_id: "r-live", status: "running", cause: { kind: "manual", excerpt: "run now" }, delivery: null })]);

    render(wrap(
      <DetailView automation={AUTOMATION} onBack={() => {}} onOpenWorkflowRun={() => {}} onAutomationSaved={async () => {}} feedShowsActivity />,
      {},
    ));
    await act(async () => {});
    expect(screen.getByText(/no runs yet/i)).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });

    expect(screen.getByTestId("run-history-row")).toBeInTheDocument();
    expect(screen.queryByText(/no runs yet/i)).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("Ejecutar ahora fires the automation and refreshes the run list", async () => {
    const listSpy = vi.mocked(api.listAutomationRuns);
    listSpy.mockClear();
    listSpy.mockResolvedValue([]);
    const user = userEvent.setup();
    render(wrap(<DetailView automation={AUTOMATION} onBack={() => {}} onOpenWorkflowRun={() => {}} onAutomationSaved={async () => {}} />, {}));

    await screen.findByText(/no runs yet/i);
    const callsBefore = listSpy.mock.calls.length;

    await user.click(screen.getByRole("button", { name: /run now|ejecutar ahora/i }));

    await waitFor(() => expect(api.fireAutomation).toHaveBeenCalledWith("tok", "soporte-guard"));
    await waitFor(() => expect(listSpy.mock.calls.length).toBeGreaterThan(callsBefore));
    expect(await screen.findByText(/r-new/)).toBeInTheDocument();
  });

  it("renders a fireAutomation API error inline instead of throwing", async () => {
    vi.mocked(api.fireAutomation).mockRejectedValueOnce(new Error("automation is disabled"));
    const user = userEvent.setup();
    render(wrap(<DetailView automation={AUTOMATION} onBack={() => {}} onOpenWorkflowRun={() => {}} onAutomationSaved={async () => {}} />, {}));

    await screen.findByText(/no runs yet/i);
    await user.click(screen.getByRole("button", { name: /run now|ejecutar ahora/i }));

    expect(await screen.findByText("automation is disabled")).toBeInTheDocument();
  });

  it("the pause/resume control shows Pause while active, flips enabled off on click, and calls onAutomationSaved", async () => {
    const onAutomationSaved = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      wrap(
        <DetailView
          automation={AUTOMATION}
          onBack={() => {}}
          onOpenWorkflowRun={() => {}}
          onAutomationSaved={onAutomationSaved}
        />,
        {},
      ),
    );

    await screen.findByText(/no runs yet/i);
    await user.click(screen.getByRole("button", { name: /^pause$|^pausar$/i }));

    await waitFor(() =>
      expect(api.saveAutomation).toHaveBeenCalledWith(
        "tok",
        expect.objectContaining({ name: "soporte-guard", enabled: false }),
      ),
    );
    await waitFor(() => expect(onAutomationSaved).toHaveBeenCalled());
  });

  it("shows Resume — not Pause — for an automation a life condition already disabled, and resuming re-arms it", async () => {
    const achievedAndDisabled: api.AutomationSummary = { ...AUTOMATION, enabled: false, achieved: true };
    const user = userEvent.setup();
    render(
      wrap(
        <DetailView
          automation={achievedAndDisabled}
          onBack={() => {}}
          onOpenWorkflowRun={() => {}}
          onAutomationSaved={async () => {}}
        />,
        {},
      ),
    );

    await screen.findByText(/no runs yet/i);
    expect(screen.queryByRole("button", { name: /^pause$|^pausar$/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^resume$|^reactivar$/i }));

    await waitFor(() =>
      expect(api.saveAutomation).toHaveBeenCalledWith(
        "tok",
        expect.objectContaining({ name: "soporte-guard", enabled: true }),
      ),
    );
  });

  it("renders a saveAutomation API error inline instead of throwing, and never calls onAutomationSaved", async () => {
    vi.mocked(api.saveAutomation).mockRejectedValueOnce(new Error("automation not found"));
    const onAutomationSaved = vi.fn();
    const user = userEvent.setup();
    render(
      wrap(
        <DetailView
          automation={AUTOMATION}
          onBack={() => {}}
          onOpenWorkflowRun={() => {}}
          onAutomationSaved={onAutomationSaved}
        />,
        {},
      ),
    );

    await screen.findByText(/no runs yet/i);
    await user.click(screen.getByRole("button", { name: /^pause$|^pausar$/i }));

    expect(await screen.findByText("automation not found")).toBeInTheDocument();
    expect(onAutomationSaved).not.toHaveBeenCalled();
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

  it("pausing from the detail view re-syncs `detail` with the refreshed list, flipping the control's label", async () => {
    vi.mocked(api.listAutomations)
      .mockResolvedValueOnce([AUTOMATION])
      .mockResolvedValue([{ ...AUTOMATION, enabled: false, achieved: false }]);
    vi.mocked(api.listAutomationRuns).mockResolvedValue([]);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />, {}));

    await user.click(await screen.findByTestId("automation-row-open"));
    await user.click(await screen.findByRole("button", { name: /^pause$|^pausar$/i }));

    await waitFor(() => expect(api.saveAutomation).toHaveBeenCalled());
    // `detail` is separate state from `automations` in AutomationsView — this
    // only passes if the refreshed list is re-synced back into it, not just
    // fetched and dropped on the floor.
    expect(await screen.findByRole("button", { name: /^resume$|^reactivar$/i })).toBeInTheDocument();
  });
});
