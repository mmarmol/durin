import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { CronSettings } from "@/components/settings/CronSettings";

const listCronJobs = vi.fn();
const fetchModelPicker = vi.fn();

vi.mock("@/lib/api", () => ({
  listCronJobs: (...a: unknown[]) => listCronJobs(...a),
  fetchModelPicker: (...a: unknown[]) => fetchModelPicker(...a),
  removeCronJob: vi.fn(),
  runCronJob: vi.fn(),
  toggleCronJob: vi.fn(),
  updateCronJob: vi.fn(),
  addCronJob: vi.fn(),
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
  run_history: [RUN_2, RUN_1], // newest first
};

describe("CronSettings – run history table", () => {
  beforeEach(() => {
    listCronJobs.mockReset().mockResolvedValue([MOCK_JOB]);
    fetchModelPicker.mockReset().mockResolvedValue([]);
  });

  it("expands to show run history rows with time, status, and duration", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Daily digest"));

    // Expand the run history section
    const historyToggle = screen.getByRole("button", { name: /history/i });
    fireEvent.click(historyToggle);

    // Both run rows must be visible
    await waitFor(() => {
      // RUN_2 (newest first) — status "error"
      expect(screen.getAllByText(/error/i).length).toBeGreaterThan(0);
      // RUN_1 — status "ok"
      expect(screen.getAllByText(/ok/i).length).toBeGreaterThan(0);
    });

    // Duration values appear
    expect(screen.getByText(/3\.2\s*s/i)).toBeInTheDocument();
    expect(screen.getByText(/1\.5\s*s/i)).toBeInTheDocument();
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

    expect(onOpenSession).toHaveBeenCalledWith(RUN_2.session_key);
  });
});
