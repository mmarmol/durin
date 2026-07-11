import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { LoopForm } from "@/components/loops/LoopForm";
import type { LoopDef } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    saveLoop: vi.fn(),
    listWorkflows: vi.fn(),
  };
});

import { listWorkflows, saveLoop } from "@/lib/api";

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

describe("LoopForm", () => {
  beforeEach(() => {
    vi.mocked(listWorkflows).mockReset().mockResolvedValue(["digest-wf"]);
    vi.mocked(saveLoop).mockReset().mockResolvedValue(undefined);
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
});
