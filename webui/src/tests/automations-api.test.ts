import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  answerAutomationRun,
  ApiError,
  deleteAutomation,
  fireAutomation,
  getAutomation,
  getAutomationsHooksSecret,
  listAllAutomationRuns,
  listAutomationRuns,
  listAutomations,
  saveAutomation,
  type AutomationDef,
  type AutomationRun,
} from "@/lib/api";

const MOCK_DEF: AutomationDef = {
  name: "digest",
  workflow: "daily-digest",
  enabled: true,
  triggers: [
    { source: "schedule", schedule: { kind: "cron", expr: "0 9 * * *" }, task: "send the digest" },
  ],
  delivery: { channel: "email", to: "team@example.com", notify: "always", silent_labels: ["NOTHING_TO_REPORT"] },
  help: {},
  concurrency: "single",
};

// A fresh "running" run — run_log.start_run always initializes these four
// keys to null (see durin/automations/run_log.py:49-66), so this mock locks
// in the present-but-null reality rather than omitting them.
const MOCK_RUN: AutomationRun = {
  automation: "digest",
  run_id: "run-1",
  status: "running",
  cause: { kind: "manual", excerpt: "" },
  started_at: 1000,
  workflow_run_id: null,
  finished_at: null,
  delivery: null,
  approval: null,
};

function errorResponse(status: number) {
  return {
    ok: false,
    status,
    clone() {
      return this;
    },
    json: async () => ({ detail: "boom" }),
  } as unknown as Response;
}

describe("automations API helpers", () => {
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

  describe("listAutomations", () => {
    it("GETs /api/v1/automations and unwraps automations", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ automations: [{ ...MOCK_DEF, active_runs: 1, paused: 0, pending_events: 2, attempts: 0, achieved: false, stuck: false }] }),
      } as Response);

      const result = await listAutomations("tok");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations",
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
      expect(result).toEqual([
        { ...MOCK_DEF, active_runs: 1, paused: 0, pending_events: 2, attempts: 0, achieved: false, stuck: false },
      ]);
    });

    it("forwards an optional base URL prefix", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ automations: [] }),
      } as Response);

      await listAutomations("tok", "http://localhost:9000");

      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:9000/api/v1/automations",
        expect.anything(),
      );
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(403));

      await expect(listAutomations("tok")).rejects.toMatchObject({
        name: "ApiError",
        status: 403,
      });
    });
  });

  describe("getAutomation", () => {
    it("GETs /api/v1/automations/:name and unwraps the definition", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ name: "digest", definition: MOCK_DEF }),
      } as Response);

      const result = await getAutomation("tok", "digest");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/digest",
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
      expect(result).toEqual(MOCK_DEF);
    });

    it("encodes the automation name in the URL", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ name: "a/b", definition: MOCK_DEF }),
      } as Response);

      await getAutomation("tok", "a/b");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/a%2Fb",
        expect.anything(),
      );
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(404));

      await expect(getAutomation("tok", "missing")).rejects.toBeInstanceOf(ApiError);
    });
  });

  describe("saveAutomation", () => {
    it("PUTs to /api/v1/automations/:name with the definition body", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ name: "digest" }),
      } as Response);

      await saveAutomation("tok", MOCK_DEF);

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/digest",
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

      await saveAutomation("tok", MOCK_DEF, "http://localhost:9000");

      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:9000/api/v1/automations/digest",
        expect.anything(),
      );
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(422));

      await expect(saveAutomation("tok", MOCK_DEF)).rejects.toBeInstanceOf(ApiError);
    });
  });

  describe("deleteAutomation", () => {
    it("DELETEs /api/v1/automations/:name", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ deleted: true }),
      } as Response);

      await deleteAutomation("tok", "digest");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/digest",
        expect.objectContaining({
          method: "DELETE",
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(404));

      await expect(deleteAutomation("tok", "missing")).rejects.toBeInstanceOf(ApiError);
    });
  });

  describe("fireAutomation", () => {
    it("POSTs to /api/v1/automations/:name/fire and returns run_id", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run: MOCK_RUN }),
      } as Response);

      const result = await fireAutomation("tok", "digest", "custom task");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/digest/fire",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
          body: JSON.stringify({ task: "custom task" }),
        }),
      );
      expect(result).toEqual({ run_id: "run-1" });
    });

    it("defaults task to an empty string", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run: MOCK_RUN }),
      } as Response);

      await fireAutomation("tok", "digest");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/digest/fire",
        expect.objectContaining({ body: JSON.stringify({ task: "" }) }),
      );
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(409));

      await expect(fireAutomation("tok", "digest")).rejects.toBeInstanceOf(ApiError);
    });
  });

  describe("answerAutomationRun", () => {
    it("POSTs to /api/v1/automations/:name/runs/:runId/answer with text+action and unwraps run", async () => {
      const answered = { ...MOCK_RUN, status: "completed" as const };
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run: answered }),
      } as Response);

      const result = await answerAutomationRun("tok", "digest", "run-1", "go ahead", "approve");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/digest/runs/run-1/answer",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
          body: JSON.stringify({ text: "go ahead", action: "approve" }),
        }),
      );
      expect(result).toEqual(answered);
    });

    it("omits action when not provided", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ run: MOCK_RUN }),
      } as Response);

      await answerAutomationRun("tok", "digest", "run-1", "go ahead");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/digest/runs/run-1/answer",
        expect.objectContaining({ body: JSON.stringify({ text: "go ahead" }) }),
      );
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(400));

      await expect(answerAutomationRun("tok", "digest", "run-1", "go ahead")).rejects.toBeInstanceOf(ApiError);
    });
  });

  describe("listAutomationRuns", () => {
    it("GETs /api/v1/automations/:name/runs?limit=... and unwraps runs", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ runs: [MOCK_RUN] }),
      } as Response);

      const result = await listAutomationRuns("tok", "digest", 20);

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/digest/runs?limit=20",
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
      expect(result).toEqual([MOCK_RUN]);
    });

    it("round-trips workflow_run_id/finished_at/delivery/approval as null, not absent, for a fresh running run", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ runs: [MOCK_RUN] }),
      } as Response);

      const [result] = await listAutomationRuns("tok", "digest");

      expect(result.workflow_run_id).toBeNull();
      expect(result.finished_at).toBeNull();
      expect(result.delivery).toBeNull();
      expect(result.approval).toBeNull();
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(500));

      await expect(listAutomationRuns("tok", "digest")).rejects.toBeInstanceOf(ApiError);
    });
  });

  describe("listAllAutomationRuns", () => {
    it("GETs /api/v1/automations/runs?limit=... and unwraps runs", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ runs: [MOCK_RUN] }),
      } as Response);

      const result = await listAllAutomationRuns("tok", 100);

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/runs?limit=100",
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

      await listAllAutomationRuns("tok", 100, "http://localhost:9000");

      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:9000/api/v1/automations/runs?limit=100",
        expect.anything(),
      );
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(500));

      await expect(listAllAutomationRuns("tok")).rejects.toBeInstanceOf(ApiError);
    });
  });

  describe("getAutomationsHooksSecret", () => {
    it("GETs /api/v1/automations/hooks-secret", async () => {
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ secret: "shh", path_template: "/api/v1/hooks/{hook}" }),
      } as Response);

      const result = await getAutomationsHooksSecret("tok");

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/automations/hooks-secret",
        expect.objectContaining({
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        }),
      );
      expect(result).toEqual({ secret: "shh", path_template: "/api/v1/hooks/{hook}" });
    });

    it("throws ApiError on failure", async () => {
      vi.mocked(fetch).mockResolvedValueOnce(errorResponse(403));

      await expect(getAutomationsHooksSecret("tok")).rejects.toBeInstanceOf(ApiError);
    });
  });
});
