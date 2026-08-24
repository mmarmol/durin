import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AutomationsView } from "@/components/AutomationsView";
import { nextFireAtMs, runsForAutomation } from "@/components/automations/ListView";
import { RunDots } from "@/components/automations/RunDots";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listAutomations: vi.fn(),
    listAllAutomationRuns: vi.fn(),
    listAutomationRuns: vi.fn(),
    listCronJobs: vi.fn(),
    listWorkflows: vi.fn(),
    saveAutomation: vi.fn(),
    deleteAutomation: vi.fn(),
    getAutomationsHooksSecret: vi.fn(),
  };
});

// AutomationsView fetches the definitions, the global run feed (for the tray
// AND every row's run dots) and the cron jobs (for next-fire) in one
// Promise.all on mount — there is no tab to hide an unstubbed fetch behind,
// so every test must stub all three or the view falls back to its error
// banner instead of rendering anything the test expects. The editor form
// (opened via New automation / Editar) additionally fetches listWorkflows
// and listAutomations again for its own selects — stub those too whenever a
// test drives the form, not just the list. DetailView (opened via a row click
// or initialDetailName) fetches listAutomationRuns independently — stubbed
// here too (default empty) so a test that reaches DetailView never leaks a
// real network call; a test asserting on its run history overrides this.
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listAutomations).mockResolvedValue([]);
  vi.mocked(api.listAllAutomationRuns).mockResolvedValue([]);
  vi.mocked(api.listAutomationRuns).mockResolvedValue([]);
  vi.mocked(api.listCronJobs).mockResolvedValue([]);
  vi.mocked(api.listWorkflows).mockResolvedValue([]);
  vi.mocked(api.saveAutomation).mockResolvedValue(undefined);
  vi.mocked(api.deleteAutomation).mockResolvedValue(undefined);
  vi.mocked(api.getAutomationsHooksSecret).mockResolvedValue({
    secret: "whsec_abc123",
    path_template: "/api/v1/hooks/{hook}",
  });
});
afterEach(() => vi.restoreAllMocks());

function wrap(children: ReactNode) {
  return (
    <ClientProvider
      client={{} as unknown as import("@/lib/durin-client").DurinClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

// A definitions-list row's own name can also appear as the automation name
// on a tray row (the tray and the list both key off AutomationRun.automation
// / AutomationSummary.name), so this can't just grab the first match — it
// has to find the one inside an actual list row.
async function rowFor(name: string) {
  const matches = await screen.findAllByText(name);
  const row = matches.map((el) => el.closest("[data-testid='automation-row']")).find((el) => el !== null);
  if (!row) throw new Error(`no automation-row found for "${name}"`);
  return within(row as HTMLElement);
}

// -- fixtures -----------------------------------------------------------

const GUARD: api.AutomationSummary = {
  name: "soporte-guard",
  workflow: "slack-ticket-pipeline",
  enabled: true,
  triggers: [
    {
      source: "channel",
      channel: "slack",
      filters: { chat: "C0GUARD" },
      correlate: "Ticket #(\\d+)",
    },
  ],
  delivery: { notify: "when_notable", silent_labels: [] },
  help: {},
  concurrency: "single",
  active_runs: 1,
  paused: 1,
  pending_events: 0,
  attempts: 0,
  achieved: false,
  stuck: false,
};

const FAC: api.AutomationSummary = {
  name: "cobrar-fac-1042",
  workflow: "cobrar-factura",
  enabled: true,
  triggers: [
    {
      source: "schedule",
      schedule: { kind: "every", every_ms: 2 * 86_400_000, at_ms: Date.UTC(2026, 0, 1, 9, 0), tz: "UTC" },
    },
  ],
  delivery: { notify: "when_notable", silent_labels: [] },
  help: {},
  life: { intent: "get paid", achieved_when: "label:COBRADA", max_attempts: 3, on_stuck: "escalate_pause" },
  concurrency: "single",
  active_runs: 0,
  paused: 1,
  pending_events: 0,
  attempts: 2,
  achieved: false,
  stuck: false,
};

const CHANGELOG: api.AutomationSummary = {
  name: "publicar-changelog",
  workflow: "changelog-post",
  enabled: true,
  triggers: [
    { source: "webhook", hook: "release", correlate: "v(\\d+\\.\\d+\\.\\d+)" },
    { source: "chain", chain_automation: "build-release", chain_when: "achieved" },
  ],
  delivery: { notify: "when_notable", silent_labels: [] },
  help: {},
  concurrency: "single",
  active_runs: 0,
  paused: 0,
  pending_events: 0,
  attempts: 0,
  achieved: false,
  stuck: false,
};

const COMPETENCIA: api.AutomationSummary = {
  name: "resumen-competencia",
  workflow: "research-competencia",
  enabled: true,
  triggers: [{ source: "schedule", schedule: { kind: "cron", expr: "0 8 * * 1" } }],
  delivery: { notify: "when_notable", silent_labels: [] },
  help: {},
  life: { intent: "stay current", achieved_when: "any_completed", max_attempts: 3, on_stuck: "escalate_pause" },
  concurrency: "single",
  active_runs: 0,
  paused: 0,
  pending_events: 0,
  attempts: 3,
  achieved: false,
  stuck: true,
};

const MIGRAR: api.AutomationSummary = {
  name: "migrar-repos",
  workflow: "migracion-git",
  enabled: false,
  triggers: [{ source: "schedule", schedule: { kind: "every", every_ms: 86_400_000 } }],
  delivery: { notify: "when_notable", silent_labels: [] },
  help: {},
  life: { intent: "migrate everything", achieved_when: "any_completed", on_stuck: "notify" },
  concurrency: "single",
  active_runs: 0,
  paused: 0,
  pending_events: 0,
  attempts: 1,
  achieved: true,
  stuck: false,
};

const BORRADOR: api.AutomationSummary = {
  name: "borrador",
  workflow: "noop",
  enabled: false,
  triggers: [],
  delivery: { notify: "never", silent_labels: [] },
  help: {},
  concurrency: "single",
  active_runs: 0,
  paused: 0,
  pending_events: 0,
  attempts: 0,
  achieved: false,
  stuck: false,
};

const ALL_AUTOMATIONS = [GUARD, FAC, CHANGELOG, COMPETENCIA, MIGRAR, BORRADOR];

function run(overrides: Partial<api.AutomationRun>): api.AutomationRun {
  return {
    automation: "soporte-guard",
    run_id: "r",
    status: "completed",
    cause: { kind: "manual", excerpt: "" },
    started_at: 1000,
    ...overrides,
  };
}

const RUN_GUARD_RUNNING = run({ automation: "soporte-guard", run_id: "r-guard-1", status: "running", started_at: 5000 });
const RUN_GUARD_DONE = run({ automation: "soporte-guard", run_id: "r-guard-2", status: "completed", started_at: 4000 });
const RUN_GUARD_FAILED = run({ automation: "soporte-guard", run_id: "r-guard-3", status: "failed", started_at: 3000 });
const RUN_GUARD_QUESTION = run({
  automation: "soporte-guard",
  run_id: "r-guard-q",
  status: "paused",
  ask_kind: "question",
  ask: "Which environment, EU or US?",
  started_at: 6000,
});

const RUN_FAC_FAILED_1 = run({ automation: "cobrar-fac-1042", run_id: "r-fac-1", status: "failed", started_at: 2000 });
const RUN_FAC_FAILED_2 = run({ automation: "cobrar-fac-1042", run_id: "r-fac-2", status: "failed", started_at: 1000 });
const RUN_FAC_APPROVAL = run({
  automation: "cobrar-fac-1042",
  run_id: "r-fac-a",
  status: "paused",
  ask_kind: "approval",
  ask: "Send a reminder to client@acme.com about invoice FAC-1042, due 08/12",
  proposal: "Subject: Invoice FAC-1042 reminder…",
  started_at: 7000,
});

const RUN_MIGRAR_ACHIEVED = run({ automation: "migrar-repos", run_id: "r-migrar-1", status: "achieved", started_at: 500 });

const ALL_RUNS = [
  RUN_GUARD_RUNNING,
  RUN_GUARD_DONE,
  RUN_GUARD_FAILED,
  RUN_GUARD_QUESTION,
  RUN_FAC_FAILED_1,
  RUN_FAC_FAILED_2,
  RUN_FAC_APPROVAL,
  RUN_MIGRAR_ACHIEVED,
];

function cronJob(overrides: Partial<api.CronJobRow>): api.CronJobRow {
  return {
    id: "j",
    name: "j",
    enabled: true,
    is_system: false,
    schedule: { kind: "cron", label: "", expr: null, every_ms: null, at_ms: null, tz: null },
    message: "",
    mode: "task",
    model: null,
    persona: null,
    channel: "web",
    automation: null,
    state: { next_run_at_ms: null, last_run_at_ms: null, last_status: null, last_error: null },
    created_at_ms: 0,
    updated_at_ms: 0,
    ...overrides,
  };
}

const CRON_FAC_LATER = cronJob({
  id: "c1",
  name: "automation:cobrar-fac-1042:0",
  automation: "cobrar-fac-1042",
  state: { next_run_at_ms: 5_000_000, last_run_at_ms: null, last_status: null, last_error: null },
});
const CRON_FAC_EARLIER = cronJob({
  id: "c2",
  name: "automation:cobrar-fac-1042:1",
  automation: "cobrar-fac-1042",
  state: { next_run_at_ms: 1_000_000, last_run_at_ms: null, last_status: null, last_error: null },
});
const CRON_COMPETENCIA = cronJob({
  id: "c3",
  name: "automation:resumen-competencia:0",
  automation: "resumen-competencia",
  state: { next_run_at_ms: 9_000_000, last_run_at_ms: null, last_status: null, last_error: null },
});
const CRON_UNRELATED = cronJob({
  id: "c4",
  name: "Standup",
  automation: null,
  state: { next_run_at_ms: 2_000_000, last_run_at_ms: null, last_status: null, last_error: null },
});

const ALL_CRON = [CRON_FAC_LATER, CRON_FAC_EARLIER, CRON_COMPETENCIA, CRON_UNRELATED];

// -- pure helpers ---------------------------------------------------------

describe("nextFireAtMs", () => {
  it("picks the earliest next_run_at_ms among an automation's own cron jobs", () => {
    expect(nextFireAtMs("cobrar-fac-1042", ALL_CRON)).toBe(1_000_000);
  });

  it("returns null when no cron job belongs to the automation", () => {
    expect(nextFireAtMs("soporte-guard", ALL_CRON)).toBeNull();
  });

  it("ignores a matching job with no next_run_at_ms", () => {
    const paused = cronJob({ automation: "x" });
    expect(nextFireAtMs("x", [paused])).toBeNull();
  });
});

describe("runsForAutomation", () => {
  it("filters to the automation and sorts newest-first", () => {
    const a = run({ run_id: "old", automation: "x", started_at: 1000 });
    const b = run({ run_id: "new", automation: "x", started_at: 3000 });
    const c = run({ run_id: "other", automation: "y", started_at: 5000 });
    expect(runsForAutomation("x", [a, b, c]).map((r) => r.run_id)).toEqual(["new", "old"]);
  });
});

// -- RunDots ---------------------------------------------------------------

describe("RunDots", () => {
  it("colors each status: primary for achieved/completed, destructive for failed, warn for rejected/interrupted, pulsing accent for running", () => {
    const runs: api.AutomationRun[] = [
      run({ run_id: "1", status: "running" }),
      run({ run_id: "2", status: "achieved" }),
      run({ run_id: "3", status: "completed" }),
      run({ run_id: "4", status: "failed" }),
      run({ run_id: "5", status: "rejected" }),
    ];
    render(<RunDots runs={runs} />);
    const dots = screen.getAllByTestId("run-dot");
    expect(dots).toHaveLength(5);
    expect(dots[0]).toHaveClass("bg-accent", "animate-pulse");
    expect(dots[1]).toHaveClass("bg-primary");
    expect(dots[2]).toHaveClass("bg-primary");
    expect(dots[3]).toHaveClass("bg-destructive");
    expect(dots[4]).toHaveClass("bg-warn");
  });

  it("shows at most the first 5 runs it is given", () => {
    const runs = Array.from({ length: 8 }, (_, i) => run({ run_id: String(i), status: "completed" }));
    render(<RunDots runs={runs} />);
    expect(screen.getAllByTestId("run-dot")).toHaveLength(5);
  });

  it("renders nothing for an automation with no runs", () => {
    const { container } = render(<RunDots runs={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

// -- AutomationsView ---------------------------------------------------------

describe("AutomationsView", () => {
  it("renders every automation's name and workflow", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    render(wrap(<AutomationsView />));

    for (const def of ALL_AUTOMATIONS) {
      await screen.findByText(def.name);
    }
    expect(screen.getByText("slack-ticket-pipeline")).toBeInTheDocument();
    expect(screen.getByText("cobrar-factura")).toBeInTheDocument();
  });

  it("renders one trigger chip per trigger, formatted by kind", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    render(wrap(<AutomationsView />));

    await screen.findByText("soporte-guard");
    expect(screen.getByText("💬 Slack · C0GUARD")).toBeInTheDocument();
    expect(screen.getByText("Ticket #(\\d+)")).toBeInTheDocument();
    expect(screen.getByText("⏰ every 2 days · 09:00")).toBeInTheDocument();
    expect(screen.getByText("🪝 webhook · release")).toBeInTheDocument();
    expect(screen.getByText("v(\\d+\\.\\d+\\.\\d+)")).toBeInTheDocument();
    expect(screen.getByText("⛓ after build-release")).toBeInTheDocument();
    expect(screen.getByText("⏰ cron · 0 8 * * 1")).toBeInTheDocument();
  });

  it("renders the life chip for every state", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    render(wrap(<AutomationsView />));

    expect((await rowFor("soporte-guard")).getByText("active")).toBeInTheDocument();
    expect((await rowFor("cobrar-fac-1042")).getByText("goal · attempt 2/3")).toBeInTheDocument();
    expect((await rowFor("resumen-competencia")).getByText("escalated")).toBeInTheDocument();
    expect((await rowFor("migrar-repos")).getByText("✓ achieved")).toBeInTheDocument();
    expect((await rowFor("borrador")).getByText("paused")).toBeInTheDocument();
  });

  it("shows the running-count chip only for automations with active runs", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    render(wrap(<AutomationsView />));

    expect((await rowFor("soporte-guard")).getByText("1 running")).toBeInTheDocument();
    expect((await rowFor("cobrar-fac-1042")).queryByText(/running/)).not.toBeInTheDocument();
  });

  it("renders run dots for a live automation but omits them once it has achieved its goal", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listAllAutomationRuns).mockResolvedValue(ALL_RUNS);
    render(wrap(<AutomationsView />));

    await screen.findByText("cobrar-factura"); // FAC's workflow name — list-only, unambiguous
    // soporte-guard + cobrar-fac-1042 both have runs and are not achieved;
    // migrar-repos also has a run (RUN_MIGRAR_ACHIEVED) but is achieved, so
    // its dots must not render even though data exists for them.
    expect(screen.getAllByTestId("run-dots")).toHaveLength(2);
    expect((await rowFor("migrar-repos")).queryByTestId("run-dots")).not.toBeInTheDocument();
  });

  it("shows a next-fire line only for automations with a matching scheduled cron job", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listCronJobs).mockResolvedValue(ALL_CRON);
    render(wrap(<AutomationsView />));

    expect((await rowFor("cobrar-fac-1042")).getByText(/^next:/)).toBeInTheDocument();
    expect((await rowFor("soporte-guard")).queryByText(/^next:/)).not.toBeInTheDocument();
  });

  it("shows an empty message when there are no automations", async () => {
    render(wrap(<AutomationsView />));
    await screen.findByText(/no automations yet/i);
  });

  it("clicking New automation opens the create form, replacing the list; Cancel returns to it", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />));

    await screen.findByText("soporte-guard");
    await user.click(screen.getByRole("button", { name: /new automation/i }));

    expect(screen.queryByText("soporte-guard")).not.toBeInTheDocument();
    const nameInput = await screen.findByLabelText(/^name/i);
    expect((nameInput as HTMLInputElement).value).toBe("");

    await user.click(screen.getByRole("button", { name: /^cancel$/i }));
    expect(await screen.findByText("soporte-guard")).toBeInTheDocument();
  });

  it("Editar on a row opens the form prefilled with that automation's own definition", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listWorkflows).mockResolvedValue(["cobrar-factura"]);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />));

    const row = await rowFor("cobrar-fac-1042");
    await user.click(row.getByRole("button", { name: /edit/i }));

    const nameInput = (await screen.findByLabelText(/^name/i)) as HTMLInputElement;
    expect(nameInput.value).toBe("cobrar-fac-1042");
    expect(nameInput).toHaveAttribute("readOnly");
    expect((screen.getByLabelText(/^workflow$/i) as HTMLSelectElement).value).toBe("cobrar-factura");
  });

  it("saving from the editor calls saveAutomation, then refreshes and returns to the list", async () => {
    // publicar-changelog (CHANGELOG): a webhook + chain trigger, both fully
    // filled by the fixture — unlike cobrar-fac-1042's schedule trigger,
    // there's no required-but-fixture-omitted field (task) that would block
    // native form submission here.
    // listAutomations is stubbed to always resolve ALL_AUTOMATIONS here: it's
    // fetched both by AutomationsView's own refresh and, independently, by
    // the form's chain-trigger automation-select — the assertion below cares
    // about the visible outcome (back on a refreshed list), not the exact
    // fetch count.
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listWorkflows).mockResolvedValue(["changelog-post"]);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />));

    const row = await rowFor("publicar-changelog");
    await user.click(row.getByRole("button", { name: /edit/i }));
    await screen.findByLabelText(/^name/i);

    await user.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(api.saveAutomation).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("publicar-changelog")).toBeInTheDocument();
    // Back on the list view proper — the editor's own "Name" field is gone.
    expect(screen.queryByLabelText(/^name/i)).not.toBeInTheDocument();
  });

  it("self-refreshes on a 30s interval, matching the sidebar badges' own cadence (F3)", async () => {
    vi.useFakeTimers();
    // vi.restoreAllMocks() in afterEach only restores vi.spyOn spies, not the
    // vi.fn() mocks vi.mock()'s factory returns — clear this mock's call
    // history explicitly so counts from earlier tests in this file can't leak
    // in and make a broken poll implementation look like it's working (same
    // precaution runs-view.test.tsx's own interval tests take).
    const listSpy = vi.mocked(api.listAutomations);
    listSpy.mockClear();
    listSpy.mockResolvedValue(ALL_AUTOMATIONS);

    render(wrap(<AutomationsView />));
    // Flush the mount effect's own fetch before advancing the fake clock —
    // otherwise there's nothing for advanceTimersByTimeAsync to settle yet.
    await act(async () => {});

    await vi.advanceTimersByTimeAsync(30_000);

    expect(listSpy.mock.calls.length).toBeGreaterThan(1);
    vi.useRealTimers();
  });
});

// -- AutomationsView initialDetailName ---------------------------------------

describe("AutomationsView initialDetailName", () => {
  it("preselects the named automation's DetailView once the list has loaded (C6's 'Abrir automatización' deep link)", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listAutomationRuns).mockResolvedValue([RUN_FAC_FAILED_1, RUN_FAC_FAILED_2]);
    render(wrap(<AutomationsView initialDetailName="cobrar-fac-1042" />));

    await screen.findByRole("button", { name: "Back" });
    expect(screen.getByText("cobrar-fac-1042")).toBeInTheDocument();
    // Left the list: its own "New automation" button is gone.
    expect(screen.queryByRole("button", { name: /new automation/i })).not.toBeInTheDocument();
    // Strengthened per F5: DetailView's own run history actually rendered
    // from the stubbed fetch, not just the header — the original assertions
    // above would pass even if listAutomationRuns leaked a real (rejecting)
    // network call, since DetailView degrades to an empty history on error.
    expect(await screen.findAllByTestId("run-history-row")).toHaveLength(2);
  });

  it("does nothing (no crash, list renders normally) when initialDetailName names an automation outside the loaded set", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    render(wrap(<AutomationsView initialDetailName="ghost-automation" />));

    await screen.findByText("soporte-guard"); // the list still renders normally
    expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();
  });
});

// -- NeedsYouTray (via AutomationsView) --------------------------------------

describe("NeedsYouTray", () => {
  it("renders an approval row and a question row", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listAllAutomationRuns).mockResolvedValue(ALL_RUNS);
    render(wrap(<AutomationsView />));

    await screen.findByText("Needs you");
    expect(screen.getByText("2 pending")).toBeInTheDocument();
    expect(screen.getByText("approval")).toBeInTheDocument();
    expect(screen.getByText("question")).toBeInTheDocument();
    expect(screen.getByText(/Send a reminder to client@acme.com/)).toBeInTheDocument();
    expect(screen.getByText(/Which environment, EU or US\?/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Answer" })).toBeInTheDocument();
  });

  it("hides the tray entirely when nothing is paused", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listAllAutomationRuns).mockResolvedValue([RUN_GUARD_DONE]);
    render(wrap(<AutomationsView />));

    await screen.findByText("soporte-guard");
    expect(screen.queryByText("Needs you")).not.toBeInTheDocument();
  });

  it("highlights the row once Review is clicked, as the selection C5 will consume", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listAllAutomationRuns).mockResolvedValue(ALL_RUNS);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />));

    const reviewButton = await screen.findByRole("button", { name: "Review" });
    const row = reviewButton.closest("[data-testid='tray-row']") as HTMLElement;
    expect(row).not.toHaveAttribute("data-selected", "true");

    await user.click(reviewButton);
    expect(row).toHaveAttribute("data-selected", "true");
  });

  it("does not leak a draft comment from one selected run into the next (F2)", async () => {
    vi.mocked(api.listAutomations).mockResolvedValue(ALL_AUTOMATIONS);
    vi.mocked(api.listAllAutomationRuns).mockResolvedValue(ALL_RUNS);
    const user = userEvent.setup();
    render(wrap(<AutomationsView />));

    await user.click(await screen.findByRole("button", { name: "Review" }));
    const approvalTextarea = await screen.findByPlaceholderText(/what to fix/i);
    await user.type(approvalTextarea, "fix the subject line");
    expect(approvalTextarea).toHaveValue("fix the subject line");

    await user.click(screen.getByRole("button", { name: "Answer" }));
    const questionTextarea = await screen.findByPlaceholderText(/your answer/i);
    expect(questionTextarea).toHaveValue("");
  });
});
