import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
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
  it("lists tasks and drills into a session on click", async () => {
    vi.mocked(api.listBackgroundTasks).mockResolvedValue([
      { kind: "subagent", id: "t1", label: "research", status: "running",
        started_at: 1, ended_at: null, session_key: "subagent:t1" },
    ]);
    const onOpenSession = vi.fn();
    render(wrap(<TasksView session="websocket:chatA" onOpenSession={onOpenSession} />));

    const row = await screen.findByText("research");
    await userEvent.click(row);
    expect(onOpenSession).toHaveBeenCalledWith("subagent:t1");
  });
});
