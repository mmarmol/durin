import { renderHook, waitFor, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listBackgroundTasks } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import type { InboundEvent } from "@/lib/types";
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

  it("live item wins over polled history for the same id", async () => {
    mockListBackgroundTasks.mockResolvedValue([
      {
        kind: "workflow",
        id: "run2",
        label: "old label",
        status: "done",
        started_at: 100,
        ended_at: 200,
        session_key: null,
      },
    ]);

    const { result, emit } = renderUseWorkState("c1", "websocket:c1");

    // Emit a live running frame for the same id
    act(() => {
      emit(workflowProgressFrame("run2", [{ id: "n1", status: "running" }]));
    });

    await waitFor(() => {
      const item = result.current.active.find((w) => w.id === "run2");
      expect(item).toBeDefined();
      expect(item!.status).toBe("running");
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
});
