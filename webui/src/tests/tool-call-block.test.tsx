import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ToolCallBlock } from "@/components/thread/ToolCallBlock";
import {
  ThreadActionsProvider,
  type ThreadActions,
} from "@/components/thread/ThreadActionsContext";
import type { ToolProgressEvent } from "@/lib/types";

/** A ThreadActions stub — pass per-test spies for the parts under test. */
function actions(overrides: Partial<ThreadActions> = {}): ThreadActions {
  return {
    sendUserMessage: vi.fn(),
    storeSecret: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

/**
 * ToolCallBlock gives the interactive tools a render of their own —
 * built from the call arguments, never leaking the internal "YIELD TO
 * USER" instruction the raw tool result carries.
 */
describe("ToolCallBlock — ask_user_question", () => {
  const askEvent: ToolProgressEvent = {
    phase: "end",
    call_id: "aq1",
    name: "ask_user_question",
    arguments: { question: "Which database?", options: ["Postgres", "SQLite"] },
    result: "Question registered (id=abc). YIELD TO USER. Present this...",
  };

  it("shows the question and the options as chips", () => {
    render(<ToolCallBlock event={askEvent} />);
    expect(screen.getByText(/❓ Which database\?/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Postgres" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "SQLite" })).toBeInTheDocument();
    expect(screen.queryByText(/YIELD TO USER/)).not.toBeInTheDocument();
  });

  it("picking an option loads it into the editable field", () => {
    render(
      <ThreadActionsProvider value={actions()}>
        <ToolCallBlock event={askEvent} />
      </ThreadActionsProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Postgres" }));
    const field = screen.getByPlaceholderText(/Type your answer/);
    expect((field as HTMLInputElement).value).toBe("Postgres");
  });

  it("submitting an (edited) answer routes through ThreadActions", () => {
    const sendUserMessage = vi.fn();
    render(
      <ThreadActionsProvider value={actions({ sendUserMessage })}>
        <ToolCallBlock event={askEvent} />
      </ThreadActionsProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Postgres" }));
    const field = screen.getByPlaceholderText(/Type your answer/);
    fireEvent.change(field, { target: { value: "Postgres 16, read replicas" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(sendUserMessage).toHaveBeenCalledWith("Postgres 16, read replicas");
    expect(screen.getByText(/Answer sent/)).toBeInTheDocument();
  });
});

describe("ToolCallBlock — request_secret", () => {
  const reqEvent: ToolProgressEvent = {
    phase: "end",
    call_id: "rs1",
    name: "request_secret",
    arguments: { name: "GH_TOKEN", service: "github", purpose: "open PRs" },
    result: "Secret 'GH_TOKEN' is not stored. YIELD TO USER. Present...",
  };

  it("without thread actions, falls back to the CLI command", () => {
    render(<ToolCallBlock event={reqEvent} />);
    expect(screen.getByText(/open PRs/)).toBeInTheDocument();
    expect(
      screen.getByText(/durin secret set GH_TOKEN --service github --scope exec/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/YIELD TO USER/)).not.toBeInTheDocument();
  });

  it("reports an already-stored credential", () => {
    render(
      <ToolCallBlock
        event={{
          ...reqEvent,
          result: "Secret 'GH_TOKEN' already exists (service=github, scope=exec).",
        }}
      />,
    );
    expect(screen.getByText(/already stored/i)).toBeInTheDocument();
    expect(screen.queryByText(/durin secret set/)).not.toBeInTheDocument();
  });

  it("the masked field stores the secret through ThreadActions", async () => {
    const storeSecret = vi.fn().mockResolvedValue(undefined);
    render(
      <ThreadActionsProvider value={actions({ storeSecret })}>
        <ToolCallBlock event={reqEvent} />
      </ThreadActionsProvider>,
    );
    const field = screen.getByPlaceholderText(/Paste the secret value/);
    expect((field as HTMLInputElement).type).toBe("password");
    fireEvent.change(field, { target: { value: "ghp_supersecret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(storeSecret).toHaveBeenCalledWith({
      name: "GH_TOKEN",
      service: "github",
      value: "ghp_supersecret",
      scope: ["exec"],
    });
    expect(await screen.findByText(/Saved/)).toBeInTheDocument();
  });
});
