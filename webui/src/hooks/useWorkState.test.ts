import { renderHook, waitFor, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listBackgroundTasks } from "@/lib/api";
import type { BackgroundTask } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import type { InboundEvent, ToolProgressEvent } from "@/lib/types";
import { useWorkState } from "./useWorkState";

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------

vi.mock("@/lib/api", () => ({ listBackgroundTasks: vi.fn() }));
vi.mock("@/providers/ClientProvider", () => ({ useClient: vi.fn() }));

const mockListBackgroundTasks = vi.mocked(listBackgroundTasks);
const mockUseClient = vi.mocked(useClient);

// ---------------------------------------------------------------------------
// Fake client helpers
// ---------------------------------------------------------------------------

function makeFakeClient() {
  let capturedHandler: ((ev: InboundEvent) => void) | null = null;
  const client = {
    onChat: vi.fn((_chatId: string, handler: (ev: InboundEvent) => void) => {
      capturedHandler = handler;
      return () => { capturedHandler = null; };
    }),
  };
  const emit = (ev: InboundEvent) => {
    if (capturedHandler) capturedHandler(ev);
  };
  return { client, emit };
}

// ---------------------------------------------------------------------------
// Frame builder helpers
// ---------------------------------------------------------------------------

type BranchFrame = { id: string; status: "running" | "done" | "failed" };
type NodeFrame = { id: string; status: "running" | "done" | "failed"; branches?: BranchFrame[] };

function workflowProgressFrame(
  runId: string,
  nodes: NodeFrame[],
  phase: "running" | "end" = "running",
): InboundEvent {
  return {
    event: "message",
    chat_id: "c1",
    text: "",
    kind: "progress",
    tool_events: [
      {
        call_id: `workflow:${runId}`,
        name: "workflow_progress",
        phase,
        arguments: { workflow: `flow-${runId}` },
        nodes: nodes.map((n) => ({
          id: n.id,
          status: n.status,
          route_label: null,
          branches: n.branches,
        })),
      },
    ],
  };
}

function subagentResultFrame(
  taskId: string,
  iteration: number,
  phase: "running" | "end" = "running",
  label?: string,
): InboundEvent {
  return {
    event: "message",
    chat_id: "c1",
    text: "",
    kind: "progress",
    tool_events: [
      {
        call_id: `subagent:${taskId}`,
        name: "subagent_result",
        phase,
        ...(label !== undefined ? { arguments: { label } } : {}),
        progress: { iteration, tool: null },
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// renderUseWorkState wrapper
// ---------------------------------------------------------------------------

function renderUseWorkState(chatId: string, sessionKey: string) {
  const { client, emit } = makeFakeClient();
  mockUseClient.mockReturnValue({
    client: client as unknown as ReturnType<typeof useClient>["client"],
    token: "tok",
    modelName: null,
    modelPreset: null,
  });
  // Default: poll returns empty list
  mockListBackgroundTasks.mockResolvedValue([]);

  const { result, unmount } = renderHook(() =>
    useWorkState(chatId, sessionKey),
  );

  return { result, emit, unmount };
}

/**
 * Render the hook and emit a single tool_events entry (a workflow_progress
 * frame) as a live WS frame. The live path updates liveRef (a ref mutation)
 * and bumps state synchronously within the same handler call, so the merge
 * is settled by the time `act` returns — no `waitFor` needed here, unlike
 * the polled path below which crosses a real microtask boundary.
 */
function renderHookWithFrame(toolEvent: ToolProgressEvent) {
  const { result, emit } = renderUseWorkState("c1", "websocket:c1");
  act(() => {
    emit({
      event: "message",
      chat_id: "c1",
      text: "",
      kind: "progress",
      tool_events: [toolEvent],
    });
  });
  return { result };
}

/**
 * Render the hook against a polled-only /api/v1/tasks response — the reload
 * case, where liveRef is empty and the poll is the only source. Waits for
 * the mocked promise chain to resolve and land in state before returning.
 */
async function renderHookWithPolled(rows: BackgroundTask[]) {
  const { client } = makeFakeClient();
  mockUseClient.mockReturnValue({
    client: client as unknown as ReturnType<typeof useClient>["client"],
    token: "tok",
    modelName: null,
    modelPreset: null,
  });
  mockListBackgroundTasks.mockResolvedValue(rows);

  const { result } = renderHook(() => useWorkState("c1", "websocket:c1"));

  await waitFor(() => {
    expect(result.current.active.length + result.current.finished.length).toBe(rows.length);
  });

  return { result };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useWorkState", () => {
  it("merges a live workflow_progress frame into an active item with branches", async () => {
    const { result, emit } = renderUseWorkState("c1", "websocket:c1");

    act(() => {
      emit(
        workflowProgressFrame("run1", [
          { id: "plan", status: "done" },
          { id: "gather", status: "running", branches: [{ id: "search", status: "running" }] },
        ]),
      );
    });

    await waitFor(() => {
      const item = result.current.active.find((w) => w.id === "run1");
      expect(item).toBeDefined();
      expect(item!.nodes?.find((n) => n.id === "gather")?.branches?.[0].status).toBe("running");
    });
  });

  it("a poll result with a done task surfaces under finished (no live counterpart)", async () => {
    const { client, emit: _emit } = makeFakeClient();
    mockUseClient.mockReturnValue({
      client: client as unknown as ReturnType<typeof useClient>["client"],
      token: "tok",
      modelName: null,
      modelPreset: null,
    });
    mockListBackgroundTasks.mockResolvedValue([
      {
        kind: "subagent",
        id: "t1",
        label: "My task",
        status: "done",
        started_at: 1000,
        ended_at: 2000,
        session_key: null,
      },
    ]);

    const { result } = renderHook(() => useWorkState("c1", "websocket:c1"));

    await waitFor(() => {
      const item = result.current.finished.find((w) => w.id === "t1");
      expect(item).toBeDefined();
      expect(item!.status).toBe("done");
    });
  });

  it("live item wins over a polled item that still says running", async () => {
    // Manual setup: renderUseWorkState would reset the poll mock to [].
    const { client, emit } = makeFakeClient();
    mockUseClient.mockReturnValue({
      client: client as unknown as ReturnType<typeof useClient>["client"],
      token: "tok",
      modelName: null,
      modelPreset: null,
    });
    mockListBackgroundTasks.mockResolvedValue([
      {
        kind: "workflow",
        id: "run2",
        label: "old label",
        status: "running",
        started_at: 100,
        ended_at: null,
        session_key: null,
      },
    ]);

    const { result } = renderHook(() => useWorkState("c1", "websocket:c1"));

    // Ensure the polled running entry is in state before the live frame lands,
    // so the merge genuinely sees both sources.
    await waitFor(() => {
      expect(result.current.active.find((w) => w.id === "run2")).toBeDefined();
    });

    // Emit a live running frame for the same id — richer (has the node tree).
    act(() => {
      emit(workflowProgressFrame("run2", [{ id: "n1", status: "running" }]));
    });

    await waitFor(() => {
      const item = result.current.active.find((w) => w.id === "run2");
      expect(item).toBeDefined();
      expect(item!.status).toBe("running");
      expect(item!.nodes?.[0]?.id).toBe("n1");
      // Partition assertion: an id must appear in exactly one of active/finished.
      expect(result.current.finished.find((w) => w.id === "run2")).toBeUndefined();
    });
  });

  it("a decided manifest beats a stale live running frame (crashed run)", async () => {
    // A crashed/reconciled run never emits a final WS frame: its last live
    // frame says "running" forever. Once the poll reports the run as decided
    // (failed here), the manifest must win or the panel and strip would show
    // the run as in-progress indefinitely.
    const { client, emit } = makeFakeClient();
    mockUseClient.mockReturnValue({
      client: client as unknown as ReturnType<typeof useClient>["client"],
      token: "tok",
      modelName: null,
      modelPreset: null,
    });
    mockListBackgroundTasks.mockResolvedValue([
      {
        kind: "workflow",
        id: "run-crashed",
        label: "flow-run-crashed",
        status: "failed",
        started_at: 100,
        ended_at: 200,
        session_key: null,
      },
    ]);

    const { result } = renderHook(() => useWorkState("c1", "websocket:c1"));

    // The stale live frame arrives (order-independent: the merge recomputes on
    // every state change, and the decided poll must win either way).
    act(() => {
      emit(workflowProgressFrame("run-crashed", [{ id: "n1", status: "running" }]));
    });

    await waitFor(() => {
      const item = result.current.finished.find((w) => w.id === "run-crashed");
      expect(item).toBeDefined();
      expect(item!.status).toBe("failed");
      expect(result.current.active.find((w) => w.id === "run-crashed")).toBeUndefined();
    });
  });

  it("subagent_result frame updates steps counter", async () => {
    const { result, emit } = renderUseWorkState("c1", "websocket:c1");

    act(() => {
      emit(subagentResultFrame("agent1", 5));
    });

    await waitFor(() => {
      const item = result.current.active.find((w) => w.id === "agent1");
      expect(item).toBeDefined();
      expect(item!.steps).toBe(5);
    });
  });

  it("subagent_result uses arguments.label when present, falls back to taskId", async () => {
    const { result, emit } = renderUseWorkState("c1", "websocket:c1");

    // Frame with a label in arguments.
    act(() => {
      emit(subagentResultFrame("agent-labeled", 1, "running", "My sub-agent"));
    });

    await waitFor(() => {
      const item = result.current.active.find((w) => w.id === "agent-labeled");
      expect(item).toBeDefined();
      expect(item!.label).toBe("My sub-agent");
    });

    // Frame without arguments — label falls back to the taskId.
    act(() => {
      emit(subagentResultFrame("agent-nolabel", 1));
    });

    await waitFor(() => {
      const item = result.current.active.find((w) => w.id === "agent-nolabel");
      expect(item).toBeDefined();
      expect(item!.label).toBe("agent-nolabel");
    });
  });

  it("workflow_progress with phase=end moves item to finished", async () => {
    const { result, emit } = renderUseWorkState("c1", "websocket:c1");

    act(() => {
      emit(workflowProgressFrame("run3", [{ id: "n1", status: "done" }], "end"));
    });

    await waitFor(() => {
      const item = result.current.finished.find((w) => w.id === "run3");
      expect(item).toBeDefined();
      expect(item!.status).toBe("done");
    });
  });

  it("a resumed run's new progress frame flips a needs_input item back to running (no sticky pause)", async () => {
    // Seed the poll with a needs_input run — this is how a paused run first
    // surfaces (the manifest status), before any live frame arrives for it.
    const { client, emit } = makeFakeClient();
    mockUseClient.mockReturnValue({
      client: client as unknown as ReturnType<typeof useClient>["client"],
      token: "tok",
      modelName: null,
      modelPreset: null,
    });
    mockListBackgroundTasks.mockResolvedValue([
      {
        kind: "workflow",
        id: "run-resume",
        label: "flow-run-resume",
        status: "needs_input",
        started_at: 1000,
        ended_at: null,
        session_key: null,
      },
    ]);

    const { result } = renderHook(() => useWorkState("c1", "websocket:c1"));

    await waitFor(() => {
      const item = result.current.active.find((w) => w.id === "run-resume");
      expect(item).toBeDefined();
      expect(item!.status).toBe("needs_input");
    });

    // The calling agent resumes the SAME run_id; the engine emits a fresh
    // "running" progress frame for it — the card must return to running, not
    // stay stuck on needs_input.
    act(() => {
      emit(workflowProgressFrame("run-resume", [{ id: "gate", status: "running" }]));
    });

    await waitFor(() => {
      const item = result.current.active.find((w) => w.id === "run-resume");
      expect(item).toBeDefined();
      expect(item!.status).toBe("running");
    });
  });

  it("refresh() triggers an immediate poll", async () => {
    mockListBackgroundTasks.mockResolvedValue([]);
    const { result } = renderUseWorkState("c1", "websocket:c1");

    // Wait for the initial poll call
    await waitFor(() => expect(mockListBackgroundTasks).toHaveBeenCalledTimes(1));

    act(() => result.current.refresh());

    await waitFor(() => expect(mockListBackgroundTasks).toHaveBeenCalledTimes(2));
  });

  it("polled workflow with nodes surfaces nodes on the finished item", async () => {
    const { client } = makeFakeClient();
    mockUseClient.mockReturnValue({
      client: client as unknown as ReturnType<typeof useClient>["client"],
      token: "tok",
      modelName: null,
      modelPreset: null,
    });
    mockListBackgroundTasks.mockResolvedValue([
      {
        kind: "workflow",
        id: "wf1",
        label: "My workflow",
        status: "done",
        started_at: 1000,
        ended_at: 2000,
        session_key: null,
        nodes: [{ id: "plan", status: "done", branches: null }],
      },
    ]);

    const { result } = renderHook(() => useWorkState("c1", "websocket:c1"));

    await waitFor(() => {
      const item = result.current.finished.find((w) => w.id === "wf1");
      expect(item).toBeDefined();
      expect(item!.nodes?.[0].id).toBe("plan");
    });
  });

  it("clears live items when chatId changes (no stale bleed)", async () => {
    const { result, emit } = renderUseWorkState("c1", "websocket:c1");

    // Emit a live running item on chat c1.
    act(() => {
      emit(workflowProgressFrame("run-stale", [{ id: "n1", status: "running" }]));
    });

    await waitFor(() => {
      expect(result.current.active.find((w) => w.id === "run-stale")).toBeDefined();
    });

    // Re-render with a different chatId — the stale live item must not survive.
    const { result: result2 } = renderHook(() =>
      useWorkState("c2", "websocket:c2"),
    );

    await waitFor(() => {
      expect(result2.current.active.find((w) => w.id === "run-stale")).toBeUndefined();
      expect(result2.current.finished.find((w) => w.id === "run-stale")).toBeUndefined();
    });
  });

  it("null chatId yields empty result and no subscription", async () => {
    const fakeclient = { onChat: vi.fn() };
    mockUseClient.mockReturnValue({
      client: fakeclient as unknown as ReturnType<typeof useClient>["client"],
      token: "tok",
      modelName: null,
      modelPreset: null,
    });
    mockListBackgroundTasks.mockResolvedValue([]);

    const { result } = renderHook(() => useWorkState(null, null));

    // Allow promise microtasks to flush
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.active).toHaveLength(0);
    expect(result.current.finished).toHaveLength(0);
    expect(mockListBackgroundTasks).not.toHaveBeenCalled();
    expect(fakeclient.onChat).not.toHaveBeenCalled();
  });

  it("carries node activity and clock from a live workflow frame", () => {
    const { result } = renderHookWithFrame({
      version: 1, phase: "running", call_id: "workflow:r1", name: "workflow_progress",
      arguments: { workflow: "ticket-stage1-context", task: "TICKET_ID=23098" },
      nodes: [
        { id: "resolve-org", label: "Resolve org", status: "done", duration_s: 119.8, typical_s: 120 },
        {
          id: "consolidate", label: "Consolidate", status: "running",
          started_at: 1700, round: 3, budget: 10,
          activity: { tool: "read_file", target: "investigation.json", at: 1712 },
        },
      ],
    });

    const nodes = result.current.active[0].nodes!;
    expect(nodes[0].durationS).toBe(119.8);
    expect(nodes[0].typicalS).toBe(120);
    expect(nodes[1].startedAt).toBe(1700);
    expect(nodes[1].round).toBe(3);
    expect(nodes[1].activity).toEqual({ tool: "read_file", target: "investigation.json", at: 1712 });
  });

  it("keeps a polled running node when no live frame has arrived", async () => {
    // The reload case: liveRef is empty, the poll is the only source.
    const { result } = await renderHookWithPolled([
      {
        kind: "workflow", id: "r1", label: "wf", status: "running",
        started_at: 100, ended_at: null, session_key: null, typical_total_s: 1080,
        nodes: [{ id: "consolidate", label: "Consolidate", status: "running", started_at: 140 }],
      },
    ]);
    expect(result.current.active[0].nodes![0].startedAt).toBe(140);
    expect(result.current.active[0].typicalTotalS).toBe(1080);
  });

  it("carries max rounds, artifacts, description and parent node from a live frame", () => {
    const { result } = renderHookWithFrame({
      call_id: "workflow:r2", name: "workflow_progress", phase: "running",
      arguments: { workflow: "flow-r2" },
      nodes: [
        {
          id: "sub-step", label: "Sub step", status: "running",
          round: 2, max_rounds: 10, artifacts: ["report.md"],
          description: "Drafts the report", parent_node: "gather",
        },
      ],
    });

    const node = result.current.active[0].nodes![0];
    expect(node.maxRounds).toBe(10);
    expect(node.artifacts).toEqual(["report.md"]);
    expect(node.description).toBe("Drafts the report");
    expect(node.parentNode).toBe("gather");
  });
});
