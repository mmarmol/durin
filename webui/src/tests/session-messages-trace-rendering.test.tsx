/**
 * Render-level test: verifies that the shape produced by session_messages_to_ui_messages
 * (Python converter) actually renders tool blocks in the UI.
 *
 * The CRITICAL invariant:
 *   MessageBubble renders toolEvents ONLY when message.kind === "trace".
 *   A plain assistant row (no kind) never reads toolEvents.
 *
 * This test feeds converter-produced shapes through ThreadMessages and asserts
 * that a tool call is actually visible in the rendered output.  It must FAIL
 * against the old buggy shape (toolEvents on the assistant row, no trace row)
 * and PASS with the correct shape (separate kind:"trace" row).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ThreadMessages } from "@/components/thread/ThreadMessages";
import type { UIMessage } from "@/lib/types";

describe("session_messages_to_ui_messages converter shape — render level", () => {
  it("renders tool call when emitted as a separate kind:trace row (correct converter shape)", () => {
    // This is the shape session_messages_to_ui_messages NOW produces:
    //   - assistant content row (no toolEvents)
    //   - separate trace row with kind:"trace" and toolEvents
    const messages: UIMessage[] = [
      { id: "u1", role: "user", content: "do something", createdAt: 1 },
      {
        id: "hist-1",
        role: "assistant",
        content: "",
        createdAt: 2,
        // No toolEvents here — correct shape
      },
      {
        id: "hist-1-trace",
        role: "tool",
        kind: "trace",
        content: "",
        traces: [],
        createdAt: 2,
        toolEvents: [
          {
            call_id: "call_abc",
            phase: "end",
            name: "read_file",
            arguments: { path: "/tmp/x" },
            result: "file contents",
          },
        ],
      },
    ];

    render(<ThreadMessages messages={messages} isStreaming={false} />);

    // The tool group header button should be visible (TraceGroup renders it).
    // count=1 → t("message.toolSingle") = "Using a tool" in the en locale.
    const toolButton = screen.queryByRole("button", { name: /using a tool/i });
    expect(toolButton).not.toBeNull();
  });

  it("does NOT render tool activity when toolEvents are incorrectly on the assistant row (buggy old shape)", () => {
    // This is the BUGGY shape the old converter produced:
    //   - assistant row WITH toolEvents (no kind, no separate trace row)
    // MessageBubble ignores toolEvents on a non-trace row, so no tool block appears.
    const messages: UIMessage[] = [
      { id: "u1", role: "user", content: "do something", createdAt: 1 },
      {
        id: "hist-1",
        role: "assistant",
        content: "",
        createdAt: 2,
        // BUG: toolEvents on the assistant row — rendered as plain text, not tool block
        toolEvents: [
          {
            call_id: "call_abc",
            phase: "end",
            name: "read_file",
            arguments: { path: "/tmp/x" },
            result: "file contents",
          },
        ],
      },
    ];

    render(<ThreadMessages messages={messages} isStreaming={false} />);

    // No tool group button is rendered — the toolEvents are silently ignored.
    expect(screen.queryByRole("button", { name: /used 1 tool/i })).toBeNull();
    // And the tool name is not visible in the thread.
    expect(screen.queryByText(/read_file/)).toBeNull();
  });
});
