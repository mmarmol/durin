import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { LoopForm } from "@/components/loops/LoopForm";
import type { LoopDef } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    saveLoop: vi.fn(),
    listWorkflows: vi.fn(),
    getHooksSecret: vi.fn(),
  };
});

import { getHooksSecret, listWorkflows, saveLoop } from "@/lib/api";

const EXISTING: LoopDef = {
  name: "digest",
  enabled: true,
  workflow: "digest-wf",
  goal: {
    intent: "send the digest",
    checks: [{ kind: "assertion", required: false, text: "sent ok" }],
  },
  triggers: [{ source: "cron", schedule: { kind: "every", every_ms: 3_600_000 } }],
  concurrency: "parallel",
  stuck_after: 7,
  operator_channel: "telegram",
  operator_to: "12345",
};

const EXISTING_CHANNEL: LoopDef = {
  name: "support",
  enabled: true,
  workflow: "digest-wf",
  goal: { intent: "resolve the ticket", checks: [] },
  triggers: [
    {
      source: "channel",
      channel: "email",
      filters: { from_contains: "@acme.com", subject_contains: "urgent" },
      semantic: "the sender is asking for a refund",
      match: "always_new",
    },
  ],
  concurrency: "single",
  stuck_after: 3,
  operator_channel: null,
  operator_to: null,
};

const EXISTING_WEBHOOK: LoopDef = {
  name: "ingest",
  enabled: true,
  workflow: "digest-wf",
  goal: { intent: "process the payload", checks: [] },
  triggers: [
    { source: "webhook", hook: "stripe-events", semantic: "the payload is a refund", correlate: "id=(\\w+)" },
  ],
  concurrency: "single",
  stuck_after: 3,
  operator_channel: null,
  operator_to: null,
};

const EXISTING_TELEGRAM: LoopDef = {
  name: "chatbot",
  enabled: true,
  workflow: "digest-wf",
  goal: { intent: "answer the question", checks: [] },
  triggers: [
    {
      source: "channel",
      channel: "telegram",
      filters: { sender_contains: "@alice", text_contains: "help" },
      semantic: "user needs support",
      match: "wake_or_new",
      correlate: "ticket-(\\d+)",
    },
  ],
  concurrency: "single",
  stuck_after: 3,
  operator_channel: null,
  operator_to: null,
};

describe("LoopForm", () => {
  beforeEach(() => {
    vi.mocked(listWorkflows).mockReset().mockResolvedValue(["digest-wf"]);
    vi.mocked(saveLoop).mockReset().mockResolvedValue(undefined);
    vi.mocked(getHooksSecret)
      .mockReset()
      .mockResolvedValue({ secret: "whsec_abc123", path_template: "/api/v1/hooks/{hook}" });
  });

  it("renders an empty form with a no-triggers hint and fetched workflow options", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    const nameInput = screen.getByLabelText(/^name/i) as HTMLInputElement;
    expect(nameInput.value).toBe("");
    expect(nameInput).not.toHaveAttribute("readOnly");

    expect(screen.getByText(/no scheduled triggers/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save as paused/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save & enable/i })).toBeInTheDocument();
  });

  it("adds a trigger row and two check rows, then submits a correctly-shaped LoopDef", async () => {
    const onDone = vi.fn();
    render(<LoopForm token="tok" editLoop={null} onDone={onDone} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "digest" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "send the digest" } });
    fireEvent.change(screen.getByLabelText(/stuck after/i), { target: { value: "5" } });

    // Trigger row (default kind is "cron"): fill the cron expression.
    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    fireEvent.change(screen.getByLabelText(/cron expression/i), { target: { value: "0 9 * * *" } });

    // Script check (default kind is "script"): fill the command.
    fireEvent.click(screen.getByRole("button", { name: /add check/i }));
    fireEvent.change(screen.getByLabelText(/^command/i), { target: { value: "curl -f https://x" } });

    // Assertion check: add another row and switch its kind.
    fireEvent.click(screen.getByRole("button", { name: /add check/i }));
    const kindSelects = screen.getAllByLabelText(/^kind$/i);
    fireEvent.change(kindSelects[1], { target: { value: "assertion" } });
    fireEvent.change(screen.getByLabelText(/assertion text/i), { target: { value: "digest sent" } });

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [token, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(token).toBe("tok");
    expect(def).toEqual({
      name: "digest",
      enabled: true,
      workflow: "digest-wf",
      goal: {
        intent: "send the digest",
        checks: [
          { kind: "script", required: true, command: "curl -f https://x" },
          { kind: "assertion", required: true, text: "digest sent" },
        ],
      },
      triggers: [{ source: "cron", schedule: { kind: "cron", expr: "0 9 * * *" } }],
      concurrency: "single",
      stuck_after: 5,
      operator_channel: null,
      operator_to: null,
    });
    expect(onDone).toHaveBeenCalled();
  });

  it("checking checks-are-sufficient emits goal.checks_sufficient=true", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "digest" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "send the digest" } });
    fireEvent.click(screen.getByLabelText(/checks are sufficient/i));

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.goal.checks_sufficient).toBe(true);
  });

  it("save as paused button is type=button (not submit) and submits enabled:false", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    const pausedBtn = screen.getByRole("button", { name: /save as paused/i }) as HTMLButtonElement;
    expect(pausedBtn.type).toBe("button");

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "n" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "do it" } });

    fireEvent.click(pausedBtn);

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.enabled).toBe(false);
  });

  it("prefills from an existing LoopDef in edit mode and keeps the name read-only", async () => {
    render(<LoopForm token="tok" editLoop={EXISTING} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    const nameInput = screen.getByLabelText(/^name/i) as HTMLInputElement;
    expect(nameInput.value).toBe("digest");
    expect(nameInput).toHaveAttribute("readOnly");

    expect((screen.getByLabelText(/workflow/i) as HTMLSelectElement).value).toBe("digest-wf");
    expect((screen.getByLabelText(/^intent/i) as HTMLTextAreaElement).value).toBe("send the digest");
    expect((screen.getByLabelText(/^concurrency/i) as HTMLSelectElement).value).toBe("parallel");
    expect((screen.getByLabelText(/stuck after/i) as HTMLInputElement).value).toBe("7");
    expect((screen.getByLabelText(/^channel$/i) as HTMLInputElement).value).toBe("telegram");
    expect((screen.getByLabelText(/recipient/i) as HTMLInputElement).value).toBe("12345");

    expect((screen.getAllByLabelText(/^schedule$/i)[0] as HTMLSelectElement).value).toBe("every");
    expect((screen.getByLabelText(/interval \(seconds\)/i) as HTMLInputElement).value).toBe("3600");

    expect((screen.getAllByLabelText(/^kind$/i)[0] as HTMLSelectElement).value).toBe("assertion");
    expect((screen.getByLabelText(/assertion text/i) as HTMLInputElement).value).toBe("sent ok");
    expect((screen.getByLabelText(/^required$/i) as HTMLInputElement).checked).toBe(false);
  });

  it("a channel trigger row submits the exact backend shape (filters/semantic/match, no schedule keys)", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "support" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "resolve the ticket" } });

    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    fireEvent.change(screen.getByLabelText(/^source$/i), { target: { value: "channel" } });
    fireEvent.change(screen.getByLabelText(/from contains/i), { target: { value: "@acme.com" } });
    // Subject contains left empty on purpose — the serialized filters object
    // must omit it entirely, not send an empty string.
    fireEvent.change(screen.getByLabelText(/semantic condition/i), {
      target: { value: "the sender is asking for a refund" },
    });
    fireEvent.change(screen.getByLabelText(/match policy/i), { target: { value: "always_new" } });

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.triggers).toEqual([
      {
        source: "channel",
        channel: "email",
        filters: { from_contains: "@acme.com" },
        semantic: "the sender is asking for a refund",
        match: "always_new",
      },
    ]);
  });

  it("switching a trigger row's source (cron/channel/webhook) drops the other shapes' keys entirely", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "n" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "do it" } });

    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    // cron -> channel: schedule fields disappear, channel fields appear.
    fireEvent.change(screen.getByLabelText(/^source$/i), { target: { value: "channel" } });
    expect(screen.queryByLabelText(/cron expression/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/match policy/i)).toBeInTheDocument();

    // channel -> webhook: channel fields disappear, hook fields appear.
    fireEvent.change(screen.getByLabelText(/^source$/i), { target: { value: "webhook" } });
    expect(screen.queryByLabelText(/match policy/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/hook name/i)).toBeInTheDocument();

    // webhook -> cron: hook fields disappear, schedule fields come back.
    fireEvent.change(screen.getByLabelText(/^source$/i), { target: { value: "cron" } });
    expect(screen.queryByLabelText(/hook name/i)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/cron expression/i), { target: { value: "0 9 * * *" } });

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.triggers).toEqual([{ source: "cron", schedule: { kind: "cron", expr: "0 9 * * *" } }]);
  });

  it("a telegram channel trigger row omits from/subject but includes sender/text/correlate", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "chatbot" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "answer the question" } });

    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    fireEvent.change(screen.getByLabelText(/^source$/i), { target: { value: "channel" } });

    const row = screen.getByLabelText(/^source$/i).closest(".flex-wrap") as HTMLElement;
    fireEvent.change(within(row).getByLabelText(/^channel$/i), { target: { value: "telegram" } });

    // from/subject only make sense for email — hidden once the channel is telegram.
    expect(screen.queryByLabelText(/from contains/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/subject contains/i)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/sender contains/i), { target: { value: "@alice" } });
    fireEvent.change(screen.getByLabelText(/text contains/i), { target: { value: "help" } });
    fireEvent.change(screen.getByLabelText(/^correlate$/i), { target: { value: "ticket-(\\d+)" } });

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.triggers).toEqual([
      {
        source: "channel",
        channel: "telegram",
        filters: { sender_contains: "@alice", text_contains: "help" },
        match: "wake_or_new",
        correlate: "ticket-(\\d+)",
      },
    ]);
  });

  it("a webhook trigger row submits {source, hook} only when semantic/correlate are empty", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "ingest" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "process the payload" } });

    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    fireEvent.change(screen.getByLabelText(/^source$/i), { target: { value: "webhook" } });
    fireEvent.change(screen.getByLabelText(/hook name/i), { target: { value: "stripe-events" } });

    expect((screen.getByLabelText(/webhook url/i) as HTMLInputElement).value).toBe(
      "/api/v1/hooks/stripe-events",
    );

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.triggers).toEqual([{ source: "webhook", hook: "stripe-events" }]);
  });

  it("a webhook trigger row includes semantic and correlate only when filled", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "ingest" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "process the payload" } });

    fireEvent.click(screen.getByRole("button", { name: /add trigger/i }));
    fireEvent.change(screen.getByLabelText(/^source$/i), { target: { value: "webhook" } });
    fireEvent.change(screen.getByLabelText(/hook name/i), { target: { value: "stripe-events" } });
    fireEvent.change(screen.getByLabelText(/semantic condition/i), {
      target: { value: "the payload is a refund" },
    });
    fireEvent.change(screen.getByLabelText(/^correlate$/i), { target: { value: "id=(\\w+)" } });

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.triggers).toEqual([
      {
        source: "webhook",
        hook: "stripe-events",
        semantic: "the payload is a refund",
        correlate: "id=(\\w+)",
      },
    ]);
  });

  it("prefills a webhook trigger from an existing LoopDef and round-trips on save", async () => {
    render(<LoopForm token="tok" editLoop={EXISTING_WEBHOOK} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    expect((screen.getByLabelText(/^source$/i) as HTMLSelectElement).value).toBe("webhook");
    expect((screen.getByLabelText(/hook name/i) as HTMLInputElement).value).toBe("stripe-events");
    expect((screen.getByLabelText(/webhook url/i) as HTMLInputElement).value).toBe(
      "/api/v1/hooks/stripe-events",
    );
    expect((screen.getByLabelText(/semantic condition/i) as HTMLInputElement).value).toBe(
      "the payload is a refund",
    );
    expect((screen.getByLabelText(/^correlate$/i) as HTMLInputElement).value).toBe("id=(\\w+)");

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));
    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.triggers).toEqual(EXISTING_WEBHOOK.triggers);
  });

  it("prefills a telegram channel trigger (sender/text/correlate) from an existing LoopDef and round-trips on save", async () => {
    render(<LoopForm token="tok" editLoop={EXISTING_TELEGRAM} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    expect((screen.getByLabelText(/^source$/i) as HTMLSelectElement).value).toBe("channel");
    const row = screen.getByLabelText(/^source$/i).closest(".flex-wrap") as HTMLElement;
    expect((within(row).getByLabelText(/^channel$/i) as HTMLSelectElement).value).toBe("telegram");
    expect(screen.queryByLabelText(/from contains/i)).not.toBeInTheDocument();
    expect((screen.getByLabelText(/sender contains/i) as HTMLInputElement).value).toBe("@alice");
    expect((screen.getByLabelText(/text contains/i) as HTMLInputElement).value).toBe("help");
    expect((screen.getByLabelText(/^correlate$/i) as HTMLInputElement).value).toBe("ticket-(\\d+)");

    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));
    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.triggers).toEqual(EXISTING_TELEGRAM.triggers);
  });

  it("fetches the hooks secret only when Show secret is clicked, and only once", async () => {
    render(<LoopForm token="tok" editLoop={EXISTING_WEBHOOK} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    expect(getHooksSecret).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /show secret/i }));

    await screen.findByText("whsec_abc123");
    expect(getHooksSecret).toHaveBeenCalledTimes(1);
  });

  it("pressing Enter in the name input submits via Save & enable", async () => {
    const onDone = vi.fn();
    render(<LoopForm token="tok" editLoop={null} onDone={onDone} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    const nameInput = screen.getByLabelText(/^name/i);
    fireEvent.change(nameInput, { target: { value: "digest" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    fireEvent.change(screen.getByLabelText(/^intent/i), { target: { value: "send the digest" } });

    fireEvent.keyDown(nameInput, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.enabled).toBe(true);
    expect(onDone).toHaveBeenCalled();
  });

  it("pressing Enter in the intent textarea does NOT submit the form", async () => {
    render(<LoopForm token="tok" editLoop={null} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "digest" } });
    fireEvent.change(screen.getByLabelText(/workflow/i), { target: { value: "digest-wf" } });
    const intent = screen.getByLabelText(/^intent/i);
    fireEvent.change(intent, { target: { value: "send the digest" } });

    fireEvent.keyDown(intent, { key: "Enter", code: "Enter" });

    expect(saveLoop).not.toHaveBeenCalled();
  });

  it("prefills a channel trigger from an existing LoopDef in edit mode", async () => {
    render(<LoopForm token="tok" editLoop={EXISTING_CHANNEL} onDone={vi.fn()} onCancel={vi.fn()} />);

    await screen.findByRole("option", { name: "digest-wf" });

    expect((screen.getByLabelText(/^source$/i) as HTMLSelectElement).value).toBe("channel");
    expect((screen.getByLabelText(/from contains/i) as HTMLInputElement).value).toBe("@acme.com");
    expect((screen.getByLabelText(/subject contains/i) as HTMLInputElement).value).toBe("urgent");
    expect((screen.getByLabelText(/semantic condition/i) as HTMLInputElement).value).toBe(
      "the sender is asking for a refund",
    );
    expect((screen.getByLabelText(/match policy/i) as HTMLSelectElement).value).toBe("always_new");

    // Round-trip: re-submitting the prefilled row reproduces the same shape.
    fireEvent.click(screen.getByRole("button", { name: /save & enable/i }));
    await waitFor(() => expect(saveLoop).toHaveBeenCalledTimes(1));
    const [, def] = vi.mocked(saveLoop).mock.calls[0];
    expect(def.triggers).toEqual(EXISTING_CHANNEL.triggers);
  });
});
