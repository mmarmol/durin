import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HoistedToolBlock, ToolChipRow } from "@/components/thread/ToolBlocks";
import {
  ThreadActionsProvider,
  type ThreadActions,
} from "@/components/thread/ThreadActionsContext";
import type { ToolProgressEvent } from "@/lib/types";

function actions(overrides: Partial<ThreadActions> = {}): ThreadActions {
  return {
    sendUserMessage: vi.fn(),
    storeSecret: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

describe("HoistedToolBlock — ask_user_question", () => {
  const event: ToolProgressEvent = {
    phase: "end",
    call_id: "c",
    name: "ask_user_question",
    arguments: { question: "Color?", options: ["red", "green"] },
  };

  it("renders an active question panel with option chips", () => {
    render(
      <ThreadActionsProvider value={actions()}>
        <HoistedToolBlock event={event} answered={false} />
      </ThreadActionsProvider>,
    );
    expect(screen.getByText(/Color\?/)).toBeInTheDocument();
    expect(screen.getByText("red")).toBeInTheDocument();
    expect(screen.getByText("green")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("renders an answered question without input affordances", () => {
    render(
      <ThreadActionsProvider value={actions()}>
        <HoistedToolBlock event={event} answered={true} />
      </ThreadActionsProvider>,
    );
    expect(screen.getByText(/Color\?/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});

describe("HoistedToolBlock — todo_write", () => {
  it("renders a checklist with status glyphs", () => {
    render(
      <HoistedToolBlock
        answered={false}
        event={{
          phase: "end",
          call_id: "c",
          name: "todo_write",
          arguments: {
            todos: [
              { content: "A", status: "completed", activeForm: "Doing A" },
              { content: "B", status: "in_progress", activeForm: "Doing B" },
              { content: "C", status: "pending", activeForm: "Doing C" },
            ],
          },
        }}
      />,
    );
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("Doing B")).toBeInTheDocument();
    expect(screen.getByText("C")).toBeInTheDocument();
  });
});

describe("HoistedToolBlock — exit_plan_mode", () => {
  it("renders the plan markdown and an approve action that sends /build", async () => {
    const a = actions();
    render(
      <ThreadActionsProvider value={a}>
        <HoistedToolBlock
          answered={false}
          event={{
            phase: "end",
            call_id: "c",
            name: "exit_plan_mode",
            arguments: { plan: "# Big Plan\n\n1. one" },
          }}
        />
      </ThreadActionsProvider>,
    );
    // MarkdownText is lazy — wait for the real renderer to mount.
    expect(await screen.findByText("Big Plan")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: /build/i });
    fireEvent.click(approve);
    expect(a.sendUserMessage).toHaveBeenCalledWith("/build");
  });

  it("hides the approve action once answered", async () => {
    render(
      <ThreadActionsProvider value={actions()}>
        <HoistedToolBlock
          answered={true}
          event={{
            phase: "end",
            call_id: "c",
            name: "exit_plan_mode",
            arguments: { plan: "# Big Plan" },
          }}
        />
      </ThreadActionsProvider>,
    );
    expect(await screen.findByText("Big Plan")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /build/i })).not.toBeInTheDocument();
  });
});

describe("ToolChipRow", () => {
  it("renders one chip per event", () => {
    render(
      <ToolChipRow
        events={[
          { phase: "end", call_id: "1", name: "cron", arguments: { action: "add", name: "daily" } },
          { phase: "end", call_id: "2", name: "message", arguments: { channel: "telegram" } },
        ]}
      />,
    );
    expect(screen.getByText(/cron/)).toBeInTheDocument();
    expect(screen.getByText(/telegram/)).toBeInTheDocument();
  });
});
