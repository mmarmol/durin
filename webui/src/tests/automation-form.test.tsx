import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AutomationForm } from "@/components/automations/AutomationForm";
import { ApiError, type AutomationDef, type AutomationSummary } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    saveAutomation: vi.fn(),
    deleteAutomation: vi.fn(),
    listWorkflows: vi.fn(),
    listAutomations: vi.fn(),
    getAutomationsHooksSecret: vi.fn(),
  };
});

import {
  deleteAutomation,
  getAutomationsHooksSecret,
  listAutomations,
  listWorkflows,
  saveAutomation,
} from "@/lib/api";

function summary(def: AutomationDef): AutomationSummary {
  return {
    ...def,
    active_runs: 0,
    paused: 0,
    pending_events: 0,
    attempts: 0,
    achieved: false,
    stuck: false,
  };
}

// Every group populated: a schedule trigger with its own task, delivery with
// channel+to+notify+silent_labels, help with channel+to, a label-based life
// condition, and a non-default concurrency ("parallel") to prove the form
// preserves a field it exposes no control for.
const FULL_EXISTING: AutomationDef = {
  name: "cobrar-fac-1042",
  workflow: "cobrar-factura",
  enabled: true,
  triggers: [
    {
      source: "schedule",
      schedule: { kind: "every", every_ms: 2 * 86_400_000 },
      task: "Reclamar la factura FAC-1042 de Acme",
    },
  ],
  delivery: { channel: "slack", to: "#finanzas", notify: "when_notable", silent_labels: ["NOTHING_TO_REPORT"] },
  help: { channel: "slack", to: "@marcelo" },
  life: { intent: "get paid", achieved_when: "label:COBRADA", max_attempts: 3, on_stuck: "escalate_pause" },
  concurrency: "parallel",
};

describe("AutomationForm", () => {
  beforeEach(() => {
    vi.mocked(listWorkflows).mockReset().mockResolvedValue(["cobrar-factura", "other-wf"]);
    vi.mocked(listAutomations)
      .mockReset()
      .mockResolvedValue([summary(FULL_EXISTING), summary({ ...FULL_EXISTING, name: "build-release" })]);
    vi.mocked(saveAutomation).mockReset().mockResolvedValue(undefined);
    vi.mocked(deleteAutomation).mockReset().mockResolvedValue(undefined);
    vi.mocked(getAutomationsHooksSecret)
      .mockReset()
      .mockResolvedValue({ secret: "whsec_abc123", path_template: "/api/v1/hooks/{hook}" });
  });

  it("renders an empty create-mode form with fetched workflow options and no delete button", async () => {
    render(<AutomationForm token="tok" editAutomation={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "cobrar-factura" });

    const nameInput = screen.getByLabelText(/^name/i) as HTMLInputElement;
    expect(nameInput.value).toBe("");
    expect(nameInput).not.toHaveAttribute("readOnly");
    expect(screen.getByText(/no triggers yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^delete$/i })).not.toBeInTheDocument();
  });

  it("builds the exact AutomationDef payload from every group on create", async () => {
    const onDone = vi.fn();
    render(<AutomationForm token="tok" editAutomation={null} onDone={onDone} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "cobrar-factura" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "cobrar-fac-1042" } });
    fireEvent.change(screen.getByLabelText(/^workflow$/i), { target: { value: "cobrar-factura" } });

    // Trigger 1: schedule (default kind is cron) + its own task.
    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    fireEvent.change(screen.getByLabelText(/cron expression/i), { target: { value: "0 9 * * *" } });
    fireEvent.change(screen.getByLabelText(/task/i), {
      target: { value: "Reclamar la factura FAC-1042" },
    });

    // Trigger 2: channel (email) with named filters, semantic, correlate, match.
    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    const sources = screen.getAllByLabelText(/^source$/i);
    fireEvent.change(sources[sources.length - 1], { target: { value: "channel" } });
    const channelSelects = screen.getAllByLabelText(/^channel$/i);
    fireEvent.change(channelSelects[channelSelects.length - 1], { target: { value: "email" } });
    fireEvent.change(screen.getByLabelText(/from contains/i), { target: { value: "cliente@acme.com" } });
    fireEvent.change(screen.getByLabelText(/subject contains/i), { target: { value: "FAC-1042" } });
    fireEvent.change(screen.getByLabelText(/semantic condition/i), {
      target: { value: "el cliente confirma el pago" },
    });
    fireEvent.change(screen.getByLabelText(/^correlate$/i), { target: { value: "FAC-(\\d+)" } });
    fireEvent.change(screen.getByLabelText(/match policy/i), { target: { value: "always_new" } });

    // Trigger 3: webhook with semantic + correlate.
    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    const sources2 = screen.getAllByLabelText(/^source$/i);
    fireEvent.change(sources2[sources2.length - 1], { target: { value: "webhook" } });
    const hookInputs = screen.getAllByLabelText(/hook name/i);
    fireEvent.change(hookInputs[hookInputs.length - 1], { target: { value: "release" } });
    const semanticInputs = screen.getAllByLabelText(/semantic condition/i);
    fireEvent.change(semanticInputs[semanticInputs.length - 1], { target: { value: "es un pago" } });
    const correlateInputs = screen.getAllByLabelText(/^correlate$/i);
    fireEvent.change(correlateInputs[correlateInputs.length - 1], { target: { value: "id=(\\w+)" } });

    // Trigger 4: chain, with its own "when" — build-release is available
    // because listAutomations was stubbed to return it (create mode has no
    // self to exclude).
    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    const sources3 = screen.getAllByLabelText(/^source$/i);
    fireEvent.change(sources3[sources3.length - 1], { target: { value: "chain" } });
    await screen.findByRole("option", { name: "build-release" });
    fireEvent.change(screen.getByLabelText(/upstream automation/i), { target: { value: "build-release" } });
    fireEvent.change(screen.getByLabelText(/^when$/i), { target: { value: "achieved" } });

    // Delivery.
    const deliveryGroup = screen.getByTestId("automation-group-delivery");
    fireEvent.change(within(deliveryGroup).getByLabelText(/channel/i), { target: { value: "slack" } });
    fireEvent.change(within(deliveryGroup).getByLabelText(/^to$/i), { target: { value: "#finanzas" } });
    await userEvent.click(within(deliveryGroup).getByRole("button", { name: /only when notable/i }));
    fireEvent.change(within(deliveryGroup).getByLabelText(/new silent label/i), {
      target: { value: "REVISAR" },
    });
    fireEvent.click(within(deliveryGroup).getByRole("button", { name: /add label/i }));

    // Help.
    const helpGroup = screen.getByTestId("automation-group-help");
    fireEvent.change(within(helpGroup).getByLabelText(/channel/i), { target: { value: "slack" } });
    fireEvent.change(within(helpGroup).getByLabelText(/^to$/i), { target: { value: "@marcelo" } });

    // Life: opt in, intent, achieved_when=label:COBRADA, max attempts, on_stuck.
    const lifeGroup = screen.getByTestId("automation-group-life");
    fireEvent.click(within(lifeGroup).getByLabelText(/exists to achieve something/i));
    fireEvent.change(within(lifeGroup).getByLabelText(/exists to achieve$/i), {
      target: { value: "La factura FAC-1042 queda cobrada" },
    });
    await userEvent.click(within(lifeGroup).getByRole("button", { name: /a run ends with label/i }));
    fireEvent.change(within(lifeGroup).getByLabelText(/^label$/i), { target: { value: "COBRADA" } });
    fireEvent.change(within(lifeGroup).getByLabelText(/max attempts/i), { target: { value: "3" } });
    await userEvent.click(within(lifeGroup).getByRole("button", { name: /^escalate & pause$/i }));

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveAutomation).toHaveBeenCalledTimes(1));
    const [token, def] = vi.mocked(saveAutomation).mock.calls[0];
    expect(token).toBe("tok");
    expect(def).toEqual({
      name: "cobrar-fac-1042",
      workflow: "cobrar-factura",
      enabled: true,
      triggers: [
        { source: "schedule", schedule: { kind: "cron", expr: "0 9 * * *" }, task: "Reclamar la factura FAC-1042" },
        {
          source: "channel",
          channel: "email",
          filters: { from_contains: "cliente@acme.com", subject_contains: "FAC-1042" },
          match: "always_new",
          semantic: "el cliente confirma el pago",
          correlate: "FAC-(\\d+)",
        },
        { source: "webhook", hook: "release", semantic: "es un pago", correlate: "id=(\\w+)" },
        { source: "chain", chain_automation: "build-release", chain_when: "achieved" },
      ],
      delivery: {
        channel: "slack",
        to: "#finanzas",
        notify: "when_notable",
        silent_labels: ["NOTHING_TO_REPORT", "REVISAR"],
      },
      help: { channel: "slack", to: "@marcelo" },
      life: {
        intent: "La factura FAC-1042 queda cobrada",
        achieved_when: "label:COBRADA",
        max_attempts: 3,
        on_stuck: "escalate_pause",
      },
      concurrency: "single",
    });
    expect(onDone).toHaveBeenCalled();
  });

  it("omits the life block entirely when the life toggle is left off", async () => {
    render(<AutomationForm token="tok" editAutomation={null} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "n" } });
    fireEvent.change(screen.getByLabelText(/^workflow$/i), { target: { value: "cobrar-factura" } });
    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveAutomation).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveAutomation).mock.calls[0];
    expect(def.life).toBeUndefined();
  });

  it("save as paused is type=button and submits enabled:false", async () => {
    render(<AutomationForm token="tok" editAutomation={null} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    const pausedBtn = screen.getByRole("button", { name: /save as paused/i }) as HTMLButtonElement;
    expect(pausedBtn.type).toBe("button");

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "n" } });
    fireEvent.change(screen.getByLabelText(/^workflow$/i), { target: { value: "cobrar-factura" } });
    fireEvent.click(pausedBtn);

    await waitFor(() => expect(saveAutomation).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveAutomation).mock.calls[0];
    expect(def.enabled).toBe(false);
  });

  it("prefills every group from an existing AutomationDef and keeps the name read-only", async () => {
    render(<AutomationForm token="tok" editAutomation={FULL_EXISTING} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    const nameInput = screen.getByLabelText(/^name/i) as HTMLInputElement;
    expect(nameInput.value).toBe("cobrar-fac-1042");
    expect(nameInput).toHaveAttribute("readOnly");

    expect((screen.getByLabelText(/^workflow$/i) as HTMLSelectElement).value).toBe("cobrar-factura");
    expect((screen.getByLabelText(/task/i) as HTMLInputElement).value).toBe("Reclamar la factura FAC-1042 de Acme");

    const deliveryGroup = screen.getByTestId("automation-group-delivery");
    expect((within(deliveryGroup).getByLabelText(/channel/i) as HTMLSelectElement).value).toBe("slack");
    expect((within(deliveryGroup).getByLabelText(/^to$/i) as HTMLInputElement).value).toBe("#finanzas");
    expect(within(deliveryGroup).getByRole("button", { name: /only when notable/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(deliveryGroup).getByText("NOTHING_TO_REPORT")).toBeInTheDocument();

    const helpGroup = screen.getByTestId("automation-group-help");
    expect((within(helpGroup).getByLabelText(/channel/i) as HTMLSelectElement).value).toBe("slack");
    expect((within(helpGroup).getByLabelText(/^to$/i) as HTMLInputElement).value).toBe("@marcelo");

    const lifeGroup = screen.getByTestId("automation-group-life");
    expect((within(lifeGroup).getByLabelText(/exists to achieve$/i) as HTMLTextAreaElement).value).toBe("get paid");
    expect(within(lifeGroup).getByRole("button", { name: /a run ends with label/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect((within(lifeGroup).getByLabelText(/^label$/i) as HTMLInputElement).value).toBe("COBRADA");
    expect((within(lifeGroup).getByLabelText(/max attempts/i) as HTMLInputElement).value).toBe("3");
    expect(within(lifeGroup).getByRole("button", { name: /^escalate & pause$/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("round-trips an edit unchanged except for one field, preserving every other group and the hidden concurrency", async () => {
    render(<AutomationForm token="tok" editAutomation={FULL_EXISTING} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    fireEvent.change(screen.getByLabelText(/^workflow$/i), { target: { value: "other-wf" } });
    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveAutomation).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveAutomation).mock.calls[0];
    expect(def).toEqual({ ...FULL_EXISTING, workflow: "other-wf" });
  });

  it("prefills and round-trips an 'at' schedule trigger without a task change (local-time datetime input)", async () => {
    // Constructed via the local Date constructor so the expected input value
    // and the round-tripped at_ms agree regardless of the runtime's timezone.
    const atMs = new Date(2026, 7, 25, 9, 30, 0, 0).getTime();
    const AT_DEF: AutomationDef = {
      ...FULL_EXISTING,
      triggers: [{ source: "schedule", schedule: { kind: "at", at_ms: atMs }, task: "one-shot task" }],
    };
    render(<AutomationForm token="tok" editAutomation={AT_DEF} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    expect((screen.getByLabelText(/^schedule kind$/i) as HTMLSelectElement).value).toBe("at");
    expect((screen.getByLabelText(/date & time/i) as HTMLInputElement).value).toBe("2026-08-25T09:30");

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));
    await waitFor(() => expect(saveAutomation).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveAutomation).mock.calls[0];
    expect(def.triggers).toEqual(AT_DEF.triggers);
  });

  it("shows the silent-labels editor only when notify is 'only when notable'", async () => {
    render(<AutomationForm token="tok" editAutomation={null} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    const deliveryGroup = screen.getByTestId("automation-group-delivery");
    expect(within(deliveryGroup).queryByLabelText(/new silent label/i)).not.toBeInTheDocument();

    await userEvent.click(within(deliveryGroup).getByRole("button", { name: /only when notable/i }));
    expect(within(deliveryGroup).getByLabelText(/new silent label/i)).toBeInTheDocument();

    await userEvent.click(within(deliveryGroup).getByRole("button", { name: /^always$/i }));
    expect(within(deliveryGroup).queryByLabelText(/new silent label/i)).not.toBeInTheDocument();
  });

  it("lets an operator remove a silent label", async () => {
    render(<AutomationForm token="tok" editAutomation={FULL_EXISTING} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    const deliveryGroup = screen.getByTestId("automation-group-delivery");
    expect(within(deliveryGroup).getByText("NOTHING_TO_REPORT")).toBeInTheDocument();
    fireEvent.click(within(deliveryGroup).getByRole("button", { name: /remove label/i }));
    expect(within(deliveryGroup).queryByText("NOTHING_TO_REPORT")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));
    await waitFor(() => expect(saveAutomation).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveAutomation).mock.calls[0];
    expect(def.delivery.silent_labels).toEqual([]);
  });

  it("excludes the automation being edited from its own chain trigger's upstream select", async () => {
    render(<AutomationForm token="tok" editAutomation={FULL_EXISTING} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    const sources = screen.getAllByLabelText(/^source$/i);
    fireEvent.change(sources[sources.length - 1], { target: { value: "chain" } });

    await screen.findByRole("option", { name: "build-release" });
    expect(screen.queryByRole("option", { name: "cobrar-fac-1042" })).not.toBeInTheDocument();
  });

  it("fetches the hooks secret only when Show secret is clicked, and only once", async () => {
    const WEBHOOK_DEF: AutomationDef = {
      ...FULL_EXISTING,
      triggers: [{ source: "webhook", hook: "release" }],
    };
    render(<AutomationForm token="tok" editAutomation={WEBHOOK_DEF} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    expect(getAutomationsHooksSecret).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /show secret/i }));

    await screen.findByText("whsec_abc123");
    expect(getAutomationsHooksSecret).toHaveBeenCalledTimes(1);
  });

  it("renders a saveAutomation validation error inline at the top of the form, never a native alert", async () => {
    const alertSpy = vi.fn();
    window.alert = alertSpy;
    vi.mocked(saveAutomation).mockRejectedValueOnce(
      new ApiError(422, "HTTP 422", "life.achieved_when must be 'any_completed' or 'label:<non-empty>'"),
    );
    render(<AutomationForm token="tok" editAutomation={null} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "n" } });
    fireEvent.change(screen.getByLabelText(/^workflow$/i), { target: { value: "cobrar-factura" } });
    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    expect(await screen.findByText(/HTTP 422: life\.achieved_when must be/)).toBeInTheDocument();
    expect(alertSpy).not.toHaveBeenCalled();
  });

  it("deletes via the shared DeleteConfirm dialog, never window.confirm, then calls onDone", async () => {
    const confirmSpy = vi.fn(() => true);
    window.confirm = confirmSpy;
    const onDone = vi.fn();
    const user = userEvent.setup();
    render(<AutomationForm token="tok" editAutomation={FULL_EXISTING} onDone={onDone} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    await user.click(screen.getByRole("button", { name: /^delete$/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/cobrar-fac-1042/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    await waitFor(() => expect(deleteAutomation).toHaveBeenCalledWith("tok", "cobrar-fac-1042"));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("cancelling the delete confirmation does not call deleteAutomation", async () => {
    const user = userEvent.setup();
    render(<AutomationForm token="tok" editAutomation={FULL_EXISTING} onDone={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    await user.click(screen.getByRole("button", { name: /^delete$/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(deleteAutomation).not.toHaveBeenCalled();
  });

  it("clicking Cancel calls onCancel without saving", async () => {
    const onCancel = vi.fn();
    render(<AutomationForm token="tok" editAutomation={null} onDone={vi.fn()} onCancel={onCancel} />);
    await screen.findByRole("option", { name: "cobrar-factura" });

    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    expect(onCancel).toHaveBeenCalled();
    expect(saveAutomation).not.toHaveBeenCalled();
  });
});
