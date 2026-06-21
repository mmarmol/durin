import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ThreadMessages } from "@/components/thread/ThreadMessages";
import type { UIMessage } from "@/lib/types";

describe("ThreadMessages", () => {
  it("groups consecutive reasoning and tool rows into one cluster before the answer", () => {
    const messages: UIMessage[] = [
      {
        id: "r1",
        role: "assistant",
        content: "",
        reasoning: "thinking",
        reasoningStreaming: false,
        isStreaming: true,
        createdAt: Date.now(),
      },
      {
        id: "t1",
        role: "tool",
        kind: "trace",
        content: "search()",
        traces: ["search()"],
        createdAt: Date.now(),
      },
      {
        id: "r2",
        role: "assistant",
        content: "",
        reasoning: "more thinking",
        reasoningStreaming: false,
        isStreaming: true,
        createdAt: Date.now(),
      },
      {
        id: "a1",
        role: "assistant",
        content: "final answer",
        createdAt: Date.now(),
      },
    ];

    const { container } = render(
      <ThreadMessages messages={messages} isStreaming={false} />,
    );
    const rows = Array.from(container.firstElementChild?.children ?? []);

    expect(rows).toHaveLength(2);
    expect(rows[0]).not.toHaveClass("mt-2", "mt-4", "mt-5");
    expect(rows[1]).toHaveClass("mt-4");
  });

  it("shows copy only on the last assistant slice before the next user turn", () => {
    const messages: UIMessage[] = [
      {
        id: "early",
        role: "assistant",
        content: "starting…",
        createdAt: 1,
      },
      {
        id: "t1",
        role: "tool",
        kind: "trace",
        content: "search()",
        traces: ["search()"],
        createdAt: 2,
      },
      {
        id: "late",
        role: "assistant",
        content: "final reply",
        createdAt: 3,
      },
    ];

    render(<ThreadMessages messages={messages} isStreaming={false} />);

    expect(screen.getAllByRole("button", { name: "Copy reply" })).toHaveLength(1);
    expect(screen.getByText("final reply")).toBeInTheDocument();
  });

  it("shows copy only on the second assistant when two text slices appear before user", () => {
    const messages: UIMessage[] = [
      { id: "a1", role: "assistant", content: "part one", createdAt: 1 },
      { id: "a2", role: "assistant", content: "part two", createdAt: 2 },
    ];
    render(<ThreadMessages messages={messages} isStreaming={false} />);
    expect(screen.getAllByRole("button", { name: "Copy reply" })).toHaveLength(1);
  });
});

describe("ThreadMessages — hoisted tool blocks", () => {
  it("hoists ask_user events out of the activity cluster", () => {
    const messages: UIMessage[] = [
      { id: "u1", role: "user", content: "hi", createdAt: 1 },
      {
        id: "t1", role: "tool", kind: "trace", content: "", createdAt: 2,
        toolEvents: [
          { phase: "end", call_id: "a", name: "read_file", arguments: { path: "x" } },
          { phase: "end", call_id: "b", name: "ask_user_question",
            arguments: { question: "Color?", options: ["red"] } },
        ],
      },
    ];
    render(<ThreadMessages messages={messages} />);
    // The question panel is visible WITHOUT expanding anything:
    expect(screen.getByText(/Color\?/)).toBeInTheDocument();
    // read_file stays behind the collapsed cluster header:
    expect(screen.queryByText(/read_file/)).not.toBeInTheDocument();
  });

  it("marks a hoisted question answered when a later user message exists", () => {
    const messages: UIMessage[] = [
      {
        id: "t1", role: "tool", kind: "trace", content: "", createdAt: 1,
        toolEvents: [
          { phase: "end", call_id: "b", name: "ask_user_question",
            arguments: { question: "Color?", options: ["red"] } },
        ],
      },
      { id: "u2", role: "user", content: "red", createdAt: 2 },
    ];
    render(<ThreadMessages messages={messages} />);
    expect(screen.getByText(/Color\?/)).toBeInTheDocument();
    // Answered: no input affordance remains on the block.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("renders chips for lifecycle tools instead of burying them", () => {
    const messages: UIMessage[] = [
      {
        id: "t1", role: "tool", kind: "trace", content: "", createdAt: 1,
        toolEvents: [
          { phase: "end", call_id: "c", name: "cron",
            arguments: { action: "add", name: "daily-report" } },
        ],
      },
    ];
    render(<ThreadMessages messages={messages} />);
    expect(screen.getByText(/daily-report/)).toBeInTheDocument();
  });
});

describe("ThreadMessages — cluster count source", () => {
  it("cluster header count matches the structured event count", () => {
    const messages: UIMessage[] = [
      { id: "t1", role: "tool", kind: "trace", content: "", createdAt: 1,
        traces: ["one-line"],
        toolEvents: [
          { phase: "end", call_id: "a", name: "read_file" },
          { phase: "end", call_id: "b", name: "grep" },
        ] },
    ];
    render(<ThreadMessages messages={messages} />);
    expect(screen.getByText(/2/)).toBeInTheDocument();
  });
});

describe("ThreadMessages — single-member cluster bypass", () => {
  it("renders a tools-only turn as one fold, not a cluster wrapping a tool group", () => {
    const messages: UIMessage[] = [
      { id: "t1", role: "tool", kind: "trace", content: "", createdAt: 1,
        toolEvents: [
          { phase: "end", call_id: "a", name: "read_file" },
          { phase: "end", call_id: "b", name: "grep" },
        ] },
    ];
    render(<ThreadMessages messages={messages} isStreaming={false} />);
    // The inner tool fold is shown…
    expect(
      screen.getByRole("button", { name: /used 2 tools/i }),
    ).toBeInTheDocument();
    // …and NOT wrapped in the redundant outer "N tool calls" cluster fold.
    expect(screen.queryByText(/tool calls/i)).not.toBeInTheDocument();
  });

  it("still clusters when reasoning and tools interleave (multi-member)", () => {
    const messages: UIMessage[] = [
      { id: "r1", role: "assistant", content: "", reasoning: "think",
        reasoningStreaming: false, createdAt: 1 },
      { id: "t1", role: "tool", kind: "trace", content: "", createdAt: 2,
        toolEvents: [{ phase: "end", call_id: "a", name: "read_file" }] },
    ];
    render(<ThreadMessages messages={messages} isStreaming={false} />);
    // Two distinct blocks → the outer cluster summary ("… tool calls") earns its keep.
    expect(screen.getByText(/tool calls/i)).toBeInTheDocument();
  });
});
