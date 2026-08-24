import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";

import { CronSettings } from "@/components/settings/CronSettings";

const listCronJobs = vi.fn();
const fetchModelPicker = vi.fn();
const listChannels = vi.fn();

// The cron test suite replaces the whole `@/lib/api` module object (no
// importOriginal) — every function CronSettings or its children touch at
// runtime must be stubbed here or the call throws "X is not a function".
// This file never opens the create/edit form, so listPersonas (only fetched
// by CronForm's own effect) is intentionally omitted, mirroring
// cron-history.test.tsx's precedent.
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

const USER_JOB = {
  id: "job-1",
  name: "Daily digest",
  enabled: true,
  is_system: false,
  schedule: { kind: "cron", label: "daily", expr: "0 9 * * *", every_ms: null, at_ms: null, tz: null },
  message: "Run daily report",
  mode: "reminder",
  model: null,
  persona: null,
  channel: "default",
  automation: null,
  state: { next_run_at_ms: null, last_run_at_ms: null, last_status: null, last_error: null },
  created_at_ms: 1000,
  updated_at_ms: 1000,
};

// Mirrors the shape durin/automations/cron_sync.py + durin/service/cron.py
// actually produce for an automation-owned schedule trigger: `name` is a
// verbose internal label ("automation <name> trigger <idx>"), never shown —
// the row's display name comes from `automation` (the plain automation
// name) instead.
const AUTOMATION_JOB = {
  id: "automation:cobrar-fac-1042:0",
  name: "automation cobrar-fac-1042 trigger 0",
  enabled: true,
  is_system: false,
  schedule: { kind: "every", label: "every 2 days", expr: null, every_ms: 172_800_000, at_ms: null, tz: null },
  message: "Reclamar la factura FAC-1042",
  mode: "task",
  model: null,
  persona: null,
  channel: "",
  automation: "cobrar-fac-1042",
  state: { next_run_at_ms: null, last_run_at_ms: null, last_status: null, last_error: null },
  created_at_ms: 1000,
  updated_at_ms: 1000,
};

const AUTOMATION_JOB_2 = {
  ...AUTOMATION_JOB,
  id: "automation:resumen-competencia:0",
  name: "automation resumen-competencia trigger 0",
  automation: "resumen-competencia",
  message: "",
};

describe("CronSettings – automation-owned rows (read-only)", () => {
  beforeEach(() => {
    listCronJobs.mockReset().mockResolvedValue([USER_JOB, AUTOMATION_JOB]);
    fetchModelPicker.mockReset().mockResolvedValue([]);
    listChannels.mockReset().mockResolvedValue([]);
  });

  it("renders an automation row read-only: 🗓 + automation name, belongs-to note, read-only badge, no edit/delete/toggle/run buttons", async () => {
    render(<CronSettings token="tok" />);
    await waitFor(() => screen.getByText("Daily digest"));

    const name = await screen.findByText("🗓 cobrar-fac-1042");
    const row = name.closest("div")!.parentElement!.parentElement!;

    expect(within(row).getByText(/belongs to the automation/i)).toBeInTheDocument();
    expect(within(row).getByText(/read only/i)).toBeInTheDocument();
    expect(within(row).queryByTitle("Run now")).not.toBeInTheDocument();
    expect(within(row).queryByTitle("Edit job")).not.toBeInTheDocument();
    expect(within(row).queryByTitle("Remove job")).not.toBeInTheDocument();
    expect(within(row).queryByText(/^(enabled|disabled)$/i)).not.toBeInTheDocument();
  });

  it("leaves a user-created job's row untouched: no badge, no 🗓 prefix, all actions present", async () => {
    render(<CronSettings token="tok" />);
    const name = await screen.findByText("Daily digest");
    const row = name.closest("div")!.parentElement!.parentElement!;

    expect(within(row).queryByText(/read only/i)).not.toBeInTheDocument();
    expect(within(row).getByTitle("Run now")).toBeInTheDocument();
    expect(within(row).getByTitle("Edit job")).toBeInTheDocument();
    expect(within(row).getByTitle("Remove job")).toBeInTheDocument();
    expect(within(row).getByText(/^enabled$/i)).toBeInTheDocument();
  });

  it("clicking 'Open automation' calls onOpenAutomation with the automation's name", async () => {
    const onOpenAutomation = vi.fn();
    render(<CronSettings token="tok" onOpenAutomation={onOpenAutomation} />);
    await screen.findByText("🗓 cobrar-fac-1042");

    fireEvent.click(screen.getByRole("button", { name: /open automation/i }));
    expect(onOpenAutomation).toHaveBeenCalledWith("cobrar-fac-1042");
  });

  it("shows the show/hide pill with the automation-job count, defaulting to shown", async () => {
    listCronJobs.mockResolvedValue([USER_JOB, AUTOMATION_JOB, AUTOMATION_JOB_2]);
    render(<CronSettings token="tok" />);

    const toggle = await screen.findByRole("button", { name: /show automation schedules \(2\)/i });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("🗓 cobrar-fac-1042")).toBeInTheDocument();
    expect(screen.getByText("🗓 resumen-competencia")).toBeInTheDocument();
  });

  it("toggling the pill hides automation rows and leaves user rows visible; toggling again restores them", async () => {
    listCronJobs.mockResolvedValue([USER_JOB, AUTOMATION_JOB, AUTOMATION_JOB_2]);
    render(<CronSettings token="tok" />);

    const toggle = await screen.findByRole("button", { name: /show automation schedules \(2\)/i });
    expect(screen.getByText("🗓 cobrar-fac-1042")).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByText("🗓 cobrar-fac-1042")).not.toBeInTheDocument();
    expect(screen.queryByText("🗓 resumen-competencia")).not.toBeInTheDocument();
    expect(screen.getByText("Daily digest")).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("🗓 cobrar-fac-1042")).toBeInTheDocument();
  });

  it("hides the pill entirely when there are no automation-owned jobs", async () => {
    listCronJobs.mockResolvedValue([USER_JOB]);
    render(<CronSettings token="tok" />);
    await screen.findByText("Daily digest");

    expect(screen.queryByText(/show automation schedules/i)).not.toBeInTheDocument();
  });
});
