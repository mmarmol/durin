import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { CronSettings } from "@/components/settings/CronSettings";

const listCronJobs = vi.fn();
const fetchModelPicker = vi.fn();
const listChannels = vi.fn();

vi.mock("@/lib/api", () => ({
  listCronJobs: (...a: unknown[]) => listCronJobs(...a),
  fetchModelPicker: (...a: unknown[]) => fetchModelPicker(...a),
  listChannels: (...a: unknown[]) => listChannels(...a),
  removeCronJob: vi.fn(),
  runCronJob: vi.fn(),
  toggleCronJob: vi.fn(),
  updateCronJob: vi.fn(),
  addCronJob: vi.fn(),
}));

// ModelSelectField uses useClient() internally.
vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ token: "tok" }),
}));

const RUN_1 = {
  run_at_ms: 1_700_000_000_000,
  status: "ok" as const,
  duration_ms: 3200,
  error: null,
  session_key: "cron:job-1:run:1700000000000",
  model: "glm-5",
  summary: null,
};

const RUN_2 = {
  run_at_ms: 1_700_000_100_000,
  status: "error" as const,
  duration_ms: 1500,
  error: "timeout",
  session_key: "cron:job-1:run:1700000100000",
  model: null,
  summary: null,
};

const MOCK_JOB = {
  id: "job-1",
  name: "Daily digest",
  enabled: true,
  is_system: false,
  schedule: {
    kind: "cron",
    label: "daily",
    expr: "0 9 * * *",
    every_ms: null,
    at_ms: null,
    tz: null,
  },
  message: "Run daily report",
  channel: "default",
  state: {
    next_run_at_ms: null,
    last_run_at_ms: 1_700_000_100_000,
    last_status: "error" as const,
    last_error: null,
  },
  created_at_ms: 1000,
  updated_at_ms: 1000,
  // Backend sends oldest-first; component must render newest-first.
  run_history: [RUN_1, RUN_2],
};

describe("CronSettings – run history table", () => {
  beforeEach(() => {
    listCronJobs.mockReset().mockResolvedValue([MOCK_JOB]);
    fetchModelPicker.mockReset().mockResolvedValue([]);
    listChannels.mockReset().mockResolvedValue([]);
  });

  it("expands to show run history rows with time, status, and duration", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Daily digest"));

    // Expand the run history section
    const historyToggle = screen.getByRole("button", { name: /history/i });
    fireEvent.click(historyToggle);

    // Both run rows must be visible
    await waitFor(() => {
      // RUN_2 (newest) — status "error"
      expect(screen.getAllByText(/error/i).length).toBeGreaterThan(0);
      // RUN_1 — status "ok"
      expect(screen.getAllByText(/ok/i).length).toBeGreaterThan(0);
    });

    // Duration values appear
    expect(screen.getByText(/3\.2\s*s/i)).toBeInTheDocument();
    expect(screen.getByText(/1\.5\s*s/i)).toBeInTheDocument();
  });

  it("renders runs newest-first (RUN_2 appears before RUN_1 in DOM)", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Daily digest"));

    const historyToggle = screen.getByRole("button", { name: /history/i });
    fireEvent.click(historyToggle);

    await waitFor(() => screen.getAllByText(/error/i));

    // Get all table rows; RUN_2 (error, 1.5s) must precede RUN_1 (ok, 3.2s).
    const rows = screen.getAllByRole("row");
    const rowTexts = rows.map((r) => r.textContent ?? "");
    const errorIdx = rowTexts.findIndex((t) => t.includes("error"));
    const okIdx = rowTexts.findIndex((t) => t.includes("ok"));
    expect(errorIdx).toBeGreaterThan(0); // not the header
    expect(okIdx).toBeGreaterThan(0);
    expect(errorIdx).toBeLessThan(okIdx);
  });

  it("renders an 'open' affordance for each run row that has a session_key", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Daily digest"));

    const historyToggle = screen.getByRole("button", { name: /history/i });
    fireEvent.click(historyToggle);

    await waitFor(() => {
      // Each run row with a session_key should have an open button
      const openButtons = screen.getAllByRole("button", { name: /open/i });
      expect(openButtons.length).toBe(2);
    });
  });

  it("shows 'no runs' message when run_history is empty", async () => {
    listCronJobs.mockResolvedValue([{ ...MOCK_JOB, run_history: [] }]);
    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Daily digest"));

    const historyToggle = screen.getByRole("button", { name: /history/i });
    fireEvent.click(historyToggle);

    await waitFor(() => {
      expect(screen.getByText(/no runs/i)).toBeInTheDocument();
    });
  });

  it("calls onOpenSession with session_key when open button is clicked", async () => {
    const onOpenSession = vi.fn();
    render(<CronSettings token="tok" onOpenSession={onOpenSession} />);
    await waitFor(() => screen.getByText("Daily digest"));

    const historyToggle = screen.getByRole("button", { name: /history/i });
    fireEvent.click(historyToggle);

    await waitFor(() => screen.getAllByRole("button", { name: /open/i }));
    const openButtons = screen.getAllByRole("button", { name: /open/i });
    fireEvent.click(openButtons[0]);

    // The first visible row is now RUN_2 (newest); its session_key is clicked.
    expect(onOpenSession).toHaveBeenCalledWith(RUN_2.session_key);
  });

  it("shows only 8 rows by default and reveals all on 'show more'", async () => {
    // Create 10 run records with distinct timestamps (newest = highest ms).
    const manyRuns = Array.from({ length: 10 }, (_, i) => ({
      run_at_ms: 1_700_000_000_000 + i * 1000,
      status: "ok" as const,
      duration_ms: 100,
      error: null,
      session_key: null,
      model: null,
      summary: null,
    }));
    listCronJobs.mockResolvedValue([{ ...MOCK_JOB, run_history: manyRuns }]);

    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Daily digest"));

    const historyToggle = screen.getByRole("button", { name: /history/i });
    fireEvent.click(historyToggle);

    await waitFor(() => screen.getByRole("button", { name: /show more/i }));

    // With 10 runs and page=8, there should be 8 data rows + 1 header = 9 rows total.
    const dataRows = screen.getAllByRole("row").slice(1); // skip header
    expect(dataRows).toHaveLength(8);

    // Click "show more" to expand.
    fireEvent.click(screen.getByRole("button", { name: /show more/i }));

    const allDataRows = screen.getAllByRole("row").slice(1);
    expect(allDataRows).toHaveLength(10);

    // "Show less" must now appear.
    expect(screen.getByRole("button", { name: /show less/i })).toBeInTheDocument();
  });
});
