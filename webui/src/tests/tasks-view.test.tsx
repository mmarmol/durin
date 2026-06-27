import { render, screen, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TasksView } from "@/components/TasksView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";
import type { DurinClient } from "@/lib/durin-client";

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof api>()), listBackgroundTasks: vi.fn() }));
afterEach(() => vi.restoreAllMocks());

function fakeClient(): DurinClient {
  return {
    onDreamProgress: () => () => {},
  } as unknown as DurinClient;
}

function wrap(node: React.ReactNode) {
  return <ClientProvider client={fakeClient()} token="tok">{node}</ClientProvider>;
}

describe("TasksView", () => {
  it("renders running and finished tasks with their labels and status chips", async () => {
    vi.mocked(api.listBackgroundTasks).mockResolvedValue([
      { kind: "subagent", id: "t1", label: "research", status: "running",
        started_at: 1, ended_at: null, session_key: "subagent:t1" },
      { kind: "workflow", id: "w1", label: "build pipeline", status: "done",
        started_at: 1, ended_at: 2, session_key: "workflow:w1" },
    ]);
    render(wrap(<TasksView session="websocket:chatA" />));

    expect(await screen.findByText("research")).toBeInTheDocument();
    expect(screen.getByText("Finished 1")).toBeInTheDocument();
  });

  it("polls listBackgroundTasks again after 4 seconds", async () => {
    vi.useFakeTimers();
    vi.mocked(api.listBackgroundTasks).mockResolvedValue([
      { kind: "subagent", id: "t1", label: "research", status: "running",
        started_at: 1, ended_at: null, session_key: "subagent:t1" },
    ]);

    render(wrap(<TasksView session="websocket:chatA" />));

    // Let the initial fetch(es) resolve (StrictMode may invoke the effect twice)
    await act(async () => { await Promise.resolve(); });
    const callsAfterMount = vi.mocked(api.listBackgroundTasks).mock.calls.length;

    // Advance past the poll interval — must fire at least one more call
    await act(async () => { vi.advanceTimersByTime(4000); await Promise.resolve(); });
    expect(vi.mocked(api.listBackgroundTasks).mock.calls.length).toBeGreaterThan(callsAfterMount);

    vi.useRealTimers();
  });
});
