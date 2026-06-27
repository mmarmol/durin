import { afterEach, describe, expect, it, vi } from "vitest";
import { listBackgroundTasks } from "@/lib/api";

afterEach(() => vi.restoreAllMocks());

describe("listBackgroundTasks", () => {
  it("requests the tasks endpoint for a session and returns the array", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ tasks: [
        { kind: "subagent", id: "t1", label: "research", status: "running",
          started_at: 1, ended_at: null, session_key: "subagent:t1" },
      ] }), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    const out = await listBackgroundTasks("tok", "websocket:chatA");
    expect(out[0].id).toBe("t1");
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/api/v1/tasks?session=websocket%3AchatA");
  });
});
