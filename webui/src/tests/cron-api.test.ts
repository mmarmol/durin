import { beforeEach, describe, expect, it, vi } from "vitest";

import { addCronJob, updateCronJob } from "@/lib/api";

const MOCK_JOB = {
  id: "job-1",
  name: "Daily digest",
  enabled: true,
  is_system: false,
  schedule: { kind: "cron", label: "daily", expr: "0 9 * * *", every_ms: null, at_ms: null, tz: null },
  message: "hello",
  channel: "default",
  state: { next_run_at_ms: null, last_run_at_ms: null, last_status: null, last_error: null },
  created_at_ms: 1000,
  updated_at_ms: 1000,
};

describe("cron API helpers", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ job: MOCK_JOB }),
      }),
    );
  });

  describe("addCronJob", () => {
    it("POSTs to /api/v1/cron with the body and Bearer token", async () => {
      const body = {
        name: "Daily digest",
        message: "hello",
        mode: "agent",
        model: null,
        schedule_kind: "cron",
        expr: "0 9 * * *",
        deliver: false,
      };

      const result = await addCronJob("tok", body);

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/cron",
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
          body: JSON.stringify(body),
        }),
      );
      expect(result).toEqual(MOCK_JOB);
    });

    it("forwards an optional base URL prefix", async () => {
      await addCronJob("tok", { name: "x", message: "m", mode: "agent", model: null, schedule_kind: "cron", deliver: false }, "http://localhost:9000");

      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:9000/api/v1/cron",
        expect.anything(),
      );
    });

    it("returns run_history when the response includes it", async () => {
      const withHistory = {
        ...MOCK_JOB,
        run_history: [
          { run_at_ms: 2000, status: "ok" as const, duration_ms: 500, error: null, session_key: "s1", model: "gpt-4", summary: "done" },
        ],
      };
      vi.mocked(fetch).mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ job: withHistory }),
      } as Response);

      const result = await addCronJob("tok", { name: "x", message: "m", mode: "agent", model: null, schedule_kind: "cron", deliver: false });

      expect(result.run_history).toHaveLength(1);
      expect(result.run_history![0].status).toBe("ok");
    });
  });

  describe("updateCronJob", () => {
    it("PATCHes to /api/v1/cron with id + partial fields and Bearer token", async () => {
      const body = { id: "job-1", name: "Updated name" };

      const result = await updateCronJob("tok", body);

      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/cron",
        expect.objectContaining({
          method: "PATCH",
          headers: expect.objectContaining({ Authorization: "Bearer tok" }),
          body: JSON.stringify(body),
        }),
      );
      expect(result).toEqual(MOCK_JOB);
    });

    it("forwards an optional base URL prefix", async () => {
      await updateCronJob("tok", { id: "job-1" }, "http://localhost:9000");

      expect(fetch).toHaveBeenCalledWith(
        "http://localhost:9000/api/v1/cron",
        expect.anything(),
      );
    });
  });
});
