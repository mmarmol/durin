import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  answerLoopRun,
  deleteLoop,
  fireLoop,
  getLoop,
  listAllLoopRuns,
  listLoopRuns,
  listLoops,
  saveLoop,
  type LoopDef,
} from "@/lib/api";

const MOCK_DEF: LoopDef = {
  name: "digest",
  enabled: true,
  workflow: "daily-digest",
  goal: { intent: "send the digest", checks: [{ kind: "assertion", required: true, text: "digest sent" }] },
  triggers: [{ source: "cron", schedule: { kind: "cron", expr: "0 9 * * *" } }],
  concurrency: "single",
  stuck_after: 3,
  operator_channel: null,
  operator_to: null,
};

const MOCK_RUN = {
  run_id: "run-1",
  loop: "digest",
  status: "running" as const,
  source: "manual",
  task: "go",
  ask: null,
  goal_reached: null,
  started_at: 1000,
  finished_at: null,
};

describe("loops API helpers", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({}),
      }),
    );
  });

  describe("listLoops", () => {
    it("GETs /api/v1/loops and unwraps loops", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ loops: [{ ...MOCK_DEF, active_runs: 1, needs_operator: 0 }] }),
      } as Response);

      const result = await listLoops("tok");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops",
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
      expect(result).toEqual([{ ...MOCK_DEF, active_runs: 1, needs_operator: 0 }]);
    });

    it("forwards an optional base URL prefix", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ loops: [] }),
      } as Response);

      await listLoops("tok", "http://localhost:9000");

      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:9000/api/v1/loops",
        expect.anything(),
      );
    });
  });

  describe("getLoop", () => {
    it("GETs /api/v1/loops/:name and unwraps the definition", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ name: "digest", definition: MOCK_DEF }),
      } as Response);

      const result = await getLoop("tok", "digest");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/digest",
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
      expect(result).toEqual(MOCK_DEF);
    });

    it("encodes the loop name in the URL", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ name: "a/b", definition: MOCK_DEF }),
      } as Response);

      await getLoop("tok", "a/b");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/a%2Fb",
        expect.anything(),
      );
    });
  });

  describe("saveLoop", () => {
    it("PUTs to /api/v1/loops/:name with the definition body", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ name: "digest" }),
      } as Response);

      await saveLoop("tok", MOCK_DEF);

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/digest",
        expect.objectContaining({
          method: "PUT",
          headers: expect.objectContaining({
            Authorization: "Bearer tok",
            "Content-Type": "application/json",
          }),
          body: JSON.stringify({ definition: MOCK_DEF }),
        }),
      );
    });

    it("forwards an optional base URL prefix", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ name: "digest" }),
      } as Response);

      await saveLoop("tok", MOCK_DEF, "http://localhost:9000");

      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:9000/api/v1/loops/digest",
        expect.anything(),
      );
    });
  });

  describe("deleteLoop", () => {
    it("DELETEs /api/v1/loops/:name", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ deleted: true }),
      } as Response);

      await deleteLoop("tok", "digest");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/digest",
        expect.objectContaining({
          method: "DELETE",
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
    });
  });

  describe("fireLoop", () => {
    it("POSTs to /api/v1/loops/:name/fire and unwraps run", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run: MOCK_RUN }),
      } as Response);

      const result = await fireLoop("tok", "digest", "custom task");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/digest/fire",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
          body: JSON.stringify({ task: "custom task" }),
        }),
      );
      expect(result).toEqual(MOCK_RUN);
    });

    it("defaults task to an empty string", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run: MOCK_RUN }),
      } as Response);

      await fireLoop("tok", "digest");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/digest/fire",
        expect.objectContaining({ body: JSON.stringify({ task: "" }) }),
      );
    });
  });

  describe("answerLoopRun", () => {
    it("POSTs to /api/v1/loops/:name/runs/:runId/answer and unwraps run", async () => {
      const answered = { ...MOCK_RUN, status: "done" as const };
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run: answered }),
      } as Response);

      const result = await answerLoopRun("tok", "digest", "run-1", "go ahead");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/digest/runs/run-1/answer",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
          body: JSON.stringify({ answer: "go ahead" }),
        }),
      );
      expect(result).toEqual(answered);
    });
  });

  describe("listLoopRuns", () => {
    it("GETs /api/v1/loops/:name/runs?limit=... and unwraps runs", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ runs: [MOCK_RUN] }),
      } as Response);

      const result = await listLoopRuns("tok", "digest", 20);

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/digest/runs?limit=20",
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
      expect(result).toEqual([MOCK_RUN]);
    });
  });

  describe("listAllLoopRuns", () => {
    it("GETs /api/v1/loops/runs?limit=... and unwraps runs", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ runs: [MOCK_RUN] }),
      } as Response);

      const result = await listAllLoopRuns("tok", 100);

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/loops/runs?limit=100",
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
      expect(result).toEqual([MOCK_RUN]);
    });

    it("forwards an optional base URL prefix", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ runs: [] }),
      } as Response);

      await listAllLoopRuns("tok", 100, "http://localhost:9000");

      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:9000/api/v1/loops/runs?limit=100",
        expect.anything(),
      );
    });
  });
});
