import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { CronSettings } from "@/components/settings/CronSettings";

const listCronJobs = vi.fn();
const addCronJob = vi.fn();
const updateCronJob = vi.fn();
const fetchModelPicker = vi.fn();

vi.mock("@/lib/api", () => ({
  listCronJobs: (...a: unknown[]) => listCronJobs(...a),
  addCronJob: (...a: unknown[]) => addCronJob(...a),
  updateCronJob: (...a: unknown[]) => updateCronJob(...a),
  fetchModelPicker: (...a: unknown[]) => fetchModelPicker(...a),
  // passthrough stubs for other imports CronSettings uses
  removeCronJob: vi.fn(),
  runCronJob: vi.fn(),
  toggleCronJob: vi.fn(),
}));

const MOCK_JOB = {
  id: "job-1",
  name: "Daily digest",
  enabled: true,
  is_system: false,
  schedule: { kind: "cron", label: "daily", expr: "0 9 * * *", every_ms: null, at_ms: null, tz: null },
  message: "Run daily report",
  mode: "reminder",
  model: null,
  channel: "default",
  state: { next_run_at_ms: null, last_run_at_ms: null, last_status: null, last_error: null },
  created_at_ms: 1000,
  updated_at_ms: 1000,
};

describe("CronSettings – create form", () => {
  beforeEach(() => {
    listCronJobs.mockReset().mockResolvedValue([MOCK_JOB]);
    addCronJob.mockReset().mockResolvedValue({ ...MOCK_JOB, id: "new-job" });
    updateCronJob.mockReset().mockResolvedValue({ ...MOCK_JOB });
    fetchModelPicker.mockReset().mockResolvedValue([
      { name: "GLM 5", provider: "zai", group: "general", role: "agent", ref: "zai/glm-5" },
    ]);
  });

  it("shows 'Add job' button at the section header", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => expect(listCronJobs).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /add job/i })).toBeInTheDocument();
  });

  it("opens the create form when 'Add job' is clicked", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => expect(listCronJobs).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /add job/i }));
    expect(screen.getByLabelText(/name/i)).toBeInTheDocument();
  });

  it("calls addCronJob with correct body on submit", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => expect(listCronJobs).toHaveBeenCalled());

    // Open form
    fireEvent.click(screen.getByRole("button", { name: /add job/i }));

    // Fill name
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "My Task" } });

    // Fill prompt
    fireEvent.change(screen.getByLabelText(/prompt/i), { target: { value: "Do the thing" } });

    // Change mode to task
    fireEvent.change(screen.getByLabelText(/^mode$/i), { target: { value: "task" } });

    // Schedule: default is cron kind, fill expr
    fireEvent.change(screen.getByLabelText(/cron expression/i), { target: { value: "0 9 * * *" } });

    // Submit
    fireEvent.click(screen.getByRole("button", { name: /^save/i }));

    await waitFor(() => expect(addCronJob).toHaveBeenCalledTimes(1));

    const [token, body] = addCronJob.mock.calls[0];
    expect(token).toBe("tok");
    expect(body.name).toBe("My Task");
    expect(body.message).toBe("Do the thing");
    expect(body.mode).toBe("task");
    expect(body.schedule_kind).toBe("cron");
    expect(body.expr).toBe("0 9 * * *");
    expect(body.deliver).toBe(false);
  });

  it("interval schedule sends schedule_kind 'every' with every_ms", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => expect(listCronJobs).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /add job/i }));
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "Pinger" } });
    fireEvent.change(screen.getByLabelText(/prompt/i), { target: { value: "ping" } });

    // Switch schedule kind to interval; its stored value is "every".
    fireEvent.change(screen.getByLabelText(/^schedule$/i), { target: { value: "every" } });
    fireEvent.change(screen.getByLabelText(/interval/i), { target: { value: "3600" } });

    fireEvent.click(screen.getByRole("button", { name: /^save/i }));

    await waitFor(() => expect(addCronJob).toHaveBeenCalledTimes(1));
    const [, body] = addCronJob.mock.calls[0];
    expect(body.schedule_kind).toBe("every");
    expect(body.every_ms).toBe(3_600_000);
    expect(body.expr).toBeNull();
  });

  it("edit form preserves mode and model", async () => {
    const taskJob = {
      ...MOCK_JOB,
      id: "job-2",
      name: "Nightly task",
      mode: "task",
      model: "zai/glm-5",
    };
    listCronJobs.mockResolvedValue([taskJob]);
    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Nightly task"));

    fireEvent.click(screen.getByRole("button", { name: /edit/i }));

    // Form populated from the job: mode = task, model = the job's ref.
    expect((screen.getByLabelText(/^mode$/i) as HTMLSelectElement).value).toBe("task");
    await waitFor(() =>
      expect((screen.getByLabelText(/^model$/i) as HTMLSelectElement).value).toBe("zai/glm-5"),
    );

    fireEvent.click(screen.getByRole("button", { name: /^save/i }));

    await waitFor(() => expect(updateCronJob).toHaveBeenCalledTimes(1));
    const [, body] = updateCronJob.mock.calls[0];
    expect(body.id).toBe("job-2");
    expect(body.mode).toBe("task");
    expect(body.model).toBe("zai/glm-5");
  });

  it("shows Edit button only on non-system jobs", async () => {
    const systemJob = {
      ...MOCK_JOB,
      id: "memory_dream",
      is_system: true,
      name: "Memory dream",
    };
    listCronJobs.mockResolvedValue([MOCK_JOB, systemJob]);
    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Daily digest"));
    // User job has Edit affordance
    expect(screen.getAllByRole("button", { name: /edit/i })).toHaveLength(1);
  });
});
