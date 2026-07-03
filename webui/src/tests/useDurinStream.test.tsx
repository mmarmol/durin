import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { useDurinStream } from "@/hooks/useDurinStream";
import type { InboundEvent, GoalStateWsPayload } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const EMPTY_MESSAGES: import("@/lib/types").UIMessage[] = [];

function fakeClient() {
  const handlers = new Map<string, Set<(ev: InboundEvent) => void>>();
  const runStartedAtByChatId = new Map<string, number>();
  const goalStateByChatId = new Map<string, GoalStateWsPayload>();

  function recordGoalStatusForRunStrip(chatId: string, ev: InboundEvent) {
    if (ev.event !== "goal_status") return;
    if (ev.status === "running" && typeof ev.started_at === "number") {
      runStartedAtByChatId.set(chatId, ev.started_at);
    } else {
      runStartedAtByChatId.delete(chatId);
    }
  }

  function recordGoalStateSnapshot(chatId: string, ev: InboundEvent) {
    if (ev.event === "goal_state") {
      goalStateByChatId.set(chatId, ev.goal_state);
      return;
    }
    if (ev.event === "turn_end" && ev.goal_state != null && typeof ev.goal_state === "object") {
      goalStateByChatId.set(chatId, ev.goal_state);
    }
  }

  return {
    client: {
      status: "open" as const,
      defaultChatId: null as string | null,
      onStatus: () => () => {},
      onError: () => () => {},
      getRunStartedAt(chatId: string) {
        const v = runStartedAtByChatId.get(chatId);
        return v === undefined ? null : v;
      },
      getGoalState(chatId: string) {
        return goalStateByChatId.get(chatId);
      },
      onChat(chatId: string, h: (ev: InboundEvent) => void) {
        let set = handlers.get(chatId);
        if (!set) {
          set = new Set();
          handlers.set(chatId, set);
        }
        set.add(h);
        return () => set!.delete(h);
      },
      sendMessage: vi.fn(),
      transcribeAudio: vi.fn().mockResolvedValue("hola"),
      newChat: vi.fn(),
      attach: vi.fn(),
      connect: vi.fn(),
      close: vi.fn(),
      updateUrl: vi.fn(),
    },
    emit(chatId: string, ev: InboundEvent) {
      recordGoalStatusForRunStrip(chatId, ev);
      recordGoalStateSnapshot(chatId, ev);
      const set = handlers.get(chatId);
      set?.forEach((h) => h(ev));
    },
  };
}

function wrap(client: ReturnType<typeof fakeClient>["client"]) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <ClientProvider
        client={client as unknown as import("@/lib/durin-client").DurinClient}
        token="tok"
      >
        {children}
      </ClientProvider>
    );
  };
}

describe("useDurinStream", () => {
  it("starts in streaming mode when history shows pending tool calls", () => {
    const fake = fakeClient();
    const initialMessages = [{
      id: "m1",
      role: "assistant" as const,
      content: "Using tools",
      createdAt: Date.now(),
    }];
    const { result } = renderHook(
      () => useDurinStream("chat-p", initialMessages, true),
      {
        wrapper: wrap(fake.client),
      },
    );

    expect(result.current.isStreaming).toBe(true);
  });

  it("transcribes on the welcome screen (no active chat) instead of rejecting 'no chat'", async () => {
    const fake = fakeClient();
    const { result } = renderHook(
      () => useDurinStream(null as unknown as string, EMPTY_MESSAGES),
      { wrapper: wrap(fake.client) },
    );

    await act(async () => {
      await expect(
        result.current.transcribeAudio([
          { data_url: "data:audio/webm;codecs=opus;base64,AAAA", name: "recording.webm" },
        ]),
      ).resolves.toBe("hola");
    });

    expect(fake.client.transcribeAudio).toHaveBeenCalledWith(
      "",
      [{ data_url: "data:audio/webm;codecs=opus;base64,AAAA", name: "recording.webm" }],
      undefined,
    );
  });

  it("collapses consecutive tool_hint frames into one trace row", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-t", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-t", {
        event: "message",
        chat_id: "chat-t",
        text: 'weather("get")',
        kind: "tool_hint",
      });
      fake.emit("chat-t", {
        event: "message",
        chat_id: "chat-t",
        text: 'search "hk weather"',
        kind: "tool_hint",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].kind).toBe("trace");
    expect(result.current.messages[0].role).toBe("tool");
    expect(result.current.messages[0].traces).toEqual([
      'weather("get")',
      'search "hk weather"',
    ]);

    act(() => {
      fake.emit("chat-t", {
        event: "message",
        chat_id: "chat-t",
        text: "## Summary",
      });
    });

    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[1].role).toBe("assistant");
    expect(result.current.messages[1].kind).toBeUndefined();
  });

  it("treats progress with arbitrary agent_ui like ordinary trace text", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-au", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });
    act(() => {
      fake.emit("chat-au", {
        event: "message",
        chat_id: "chat-au",
        text: "progress · panel tick",
        kind: "progress",
        agent_ui: {
          kind: "panel",
          data: { version: 1, event: "tick", id: "x1" },
        },
      });
    });
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].kind).toBe("trace");
    expect(result.current.messages[0].content).toContain("panel tick");
  });

  it("renders live tool traces from structured tool events", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-tool-events", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-tool-events", {
        event: "message",
        chat_id: "chat-tool-events",
        text: 'search "hermes"',
        kind: "tool_hint",
        tool_events: [
          {
            phase: "start",
            name: "web_search",
            arguments: { query: "NousResearch hermes-agent", count: 8 },
          },
          {
            phase: "start",
            name: "web_search",
            arguments: { query: "hermes-agent GitHub stars", count: 8 },
          },
        ],
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].traces).toEqual([
      'web_search({"query":"NousResearch hermes-agent","count":8})',
      'web_search({"query":"hermes-agent GitHub stars","count":8})',
    ]);
    expect(result.current.messages[0].content).toBe(
      'web_search({"query":"hermes-agent GitHub stars","count":8})',
    );
  });

  it("merges a blocking tool's end frame into the existing row after an answer bubble", () => {
    // Blocking ask_user: the start frame creates the question row, the user's
    // optimistic answer bubble is inserted, then the end frame arrives. The
    // end frame must merge into the SAME row (by call_id), not spawn a
    // duplicate question block after the answer.
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-block", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-block", {
        event: "message",
        chat_id: "chat-block",
        text: "ask_user_question(...)",
        kind: "tool_hint",
        tool_events: [
          {
            phase: "start",
            call_id: "q1",
            name: "ask_user_question",
            arguments: { question: "¿Qué color?", options: ["Rojo", "Verde"] },
          },
        ],
      });
    });
    act(() => {
      result.current.send("Rojo");
    });
    act(() => {
      fake.emit("chat-block", {
        event: "message",
        chat_id: "chat-block",
        text: "",
        kind: "tool_hint",
        tool_events: [
          { phase: "end", call_id: "q1", name: "ask_user_question", result: "ok" },
        ],
      });
    });

    const traces = result.current.messages.filter((m) => m.kind === "trace");
    expect(traces).toHaveLength(1);
    const events = traces[0].toolEvents ?? [];
    expect(events).toHaveLength(1);
    expect(events[0].call_id).toBe("q1");
    expect(events[0].phase).toBe("end");
    // start args survive the merge so the panel keeps its question.
    expect((events[0].arguments as { question?: string }).question).toBe("¿Qué color?");
  });

  it("accumulates reasoning_delta chunks on a placeholder until reasoning_end", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-r", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-r", {
        event: "reasoning_delta",
        chat_id: "chat-r",
        text: "Let me think ",
      });
      fake.emit("chat-r", {
        event: "reasoning_delta",
        chat_id: "chat-r",
        text: "step by step.",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].role).toBe("assistant");
    expect(result.current.messages[0].reasoning).toBe("Let me think step by step.");
    expect(result.current.messages[0].reasoningStreaming).toBe(true);

    act(() => {
      fake.emit("chat-r", { event: "reasoning_end", chat_id: "chat-r" });
    });

    expect(result.current.messages[0].reasoningStreaming).toBe(false);
    expect(result.current.messages[0].reasoning).toBe("Let me think step by step.");
  });

  it("absorbs a streaming reasoning placeholder into the answer turn that follows", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-r2", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-r2", {
        event: "reasoning_delta",
        chat_id: "chat-r2",
        text: "Plan first.",
      });
      fake.emit("chat-r2", { event: "reasoning_end", chat_id: "chat-r2" });
      fake.emit("chat-r2", {
        event: "delta",
        chat_id: "chat-r2",
        text: "The answer is 42.",
      });
      fake.emit("chat-r2", { event: "stream_end", chat_id: "chat-r2" });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("The answer is 42.");
    expect(result.current.messages[0].reasoning).toBe("Plan first.");
    expect(result.current.messages[0].reasoningStreaming).toBe(false);
  });

  it("ignores empty reasoning_delta frames", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-r3", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-r3", {
        event: "reasoning_delta",
        chat_id: "chat-r3",
        text: "",
      });
    });

    expect(result.current.messages).toHaveLength(0);
  });

  it("treats legacy kind=reasoning messages as a complete delta + end pair", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-r4", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-r4", {
        event: "message",
        chat_id: "chat-r4",
        text: "one-shot reasoning",
        kind: "reasoning",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].reasoning).toBe("one-shot reasoning");
    expect(result.current.messages[0].reasoningStreaming).toBe(false);
  });

  it("attaches post-hoc reasoning to the same assistant turn above the answer", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-r5", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-r5", {
        event: "delta",
        chat_id: "chat-r5",
        text: "hi~",
      });
      fake.emit("chat-r5", { event: "stream_end", chat_id: "chat-r5" });
      fake.emit("chat-r5", {
        event: "reasoning_delta",
        chat_id: "chat-r5",
        text: "This reasoning arrived after the answer stream.",
      });
      fake.emit("chat-r5", { event: "reasoning_end", chat_id: "chat-r5" });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("hi~");
    expect(result.current.messages[0].reasoning).toBe(
      "This reasoning arrived after the answer stream.",
    );
    expect(result.current.messages[0].reasoningStreaming).toBe(false);
  });

  it("does not attach a new turn's reasoning across the latest user boundary", () => {
    const fake = fakeClient();
    const initialMessages = [
      {
        id: "a-prev",
        role: "assistant" as const,
        content: "Previous answer.",
        reasoning: "Previous thought.",
        createdAt: Date.now(),
      },
      {
        id: "u-next",
        role: "user" as const,
        content: "Next question",
        createdAt: Date.now(),
      },
    ];
    const { result } = renderHook(
      () => useDurinStream("chat-r6", initialMessages),
      { wrapper: wrap(fake.client) },
    );

    act(() => {
      fake.emit("chat-r6", {
        event: "reasoning_delta",
        chat_id: "chat-r6",
        text: "New turn thinking.",
      });
    });

    expect(result.current.messages).toHaveLength(3);
    expect(result.current.messages[0].reasoning).toBe("Previous thought.");
    expect(result.current.messages[2].role).toBe("assistant");
    expect(result.current.messages[2].content).toBe("");
    expect(result.current.messages[2].reasoning).toBe("New turn thinking.");
    expect(result.current.messages[2].reasoningStreaming).toBe(true);
  });

  it("does not attach reasoning across a tool trace boundary", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-r7", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-r7", {
        event: "reasoning_delta",
        chat_id: "chat-r7",
        text: "First reasoning.",
      });
      fake.emit("chat-r7", { event: "reasoning_end", chat_id: "chat-r7" });
      fake.emit("chat-r7", {
        event: "message",
        chat_id: "chat-r7",
        text: "web_search({\"query\":\"OpenClaw\"})",
        kind: "tool_hint",
      });
      fake.emit("chat-r7", {
        event: "reasoning_delta",
        chat_id: "chat-r7",
        text: "Second reasoning.",
      });
    });

    expect(result.current.messages).toHaveLength(3);
    expect(result.current.messages.map((m) => m.kind ?? "message")).toEqual([
      "message",
      "trace",
      "message",
    ]);
    expect(result.current.messages[0].reasoning).toBe("First reasoning.");
    expect(result.current.messages[1].traces).toEqual([
      "web_search({\"query\":\"OpenClaw\"})",
    ]);
    expect(result.current.messages[2].reasoning).toBe("Second reasoning.");
  });

  it("keeps tool-call reasoning before the matching live tool trace", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-tool-reasoning", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-tool-reasoning", {
        event: "reasoning_delta",
        chat_id: "chat-tool-reasoning",
        text: "I should search first.",
      });
      fake.emit("chat-tool-reasoning", {
        event: "reasoning_end",
        chat_id: "chat-tool-reasoning",
      });
      fake.emit("chat-tool-reasoning", {
        event: "message",
        chat_id: "chat-tool-reasoning",
        text: "web_search({\"query\":\"hermes\"})",
        kind: "tool_hint",
      });
      fake.emit("chat-tool-reasoning", {
        event: "turn_end",
        chat_id: "chat-tool-reasoning",
      });
    });

    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toMatchObject({
      role: "assistant",
      content: "",
      reasoning: "I should search first.",
      reasoningStreaming: false,
      isStreaming: false,
    });
    expect(result.current.messages[1]).toMatchObject({
      role: "tool",
      kind: "trace",
      traces: ["web_search({\"query\":\"hermes\"})"],
    });
  });

  it("absorbs non-streamed final answers into the preceding reasoning placeholder", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-final-reasoning", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-final-reasoning", {
        event: "message",
        chat_id: "chat-final-reasoning",
        text: "web_search({\"query\":\"hermes\"})",
        kind: "tool_hint",
      });
      fake.emit("chat-final-reasoning", {
        event: "reasoning_delta",
        chat_id: "chat-final-reasoning",
        text: "Got results; now summarize.",
      });
      fake.emit("chat-final-reasoning", {
        event: "reasoning_end",
        chat_id: "chat-final-reasoning",
      });
      fake.emit("chat-final-reasoning", {
        event: "message",
        chat_id: "chat-final-reasoning",
        text: "Hermes is an open-source agent project.",
      });
      fake.emit("chat-final-reasoning", {
        event: "turn_end",
        chat_id: "chat-final-reasoning",
      });
    });

    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toMatchObject({
      role: "tool",
      kind: "trace",
    });
    expect(result.current.messages[1]).toMatchObject({
      role: "assistant",
      content: "Hermes is an open-source agent project.",
      reasoning: "Got results; now summarize.",
      reasoningStreaming: false,
      isStreaming: false,
    });
  });

  it("prunes reasoning-only placeholders when a turn ends without an answer", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-empty-thinking", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-empty-thinking", {
        event: "reasoning_delta",
        chat_id: "chat-empty-thinking",
        text: "thinking without final text",
      });
      fake.emit("chat-empty-thinking", {
        event: "reasoning_end",
        chat_id: "chat-empty-thinking",
      });
      fake.emit("chat-empty-thinking", {
        event: "turn_end",
        chat_id: "chat-empty-thinking",
      });
    });

    expect(result.current.messages).toHaveLength(0);
    expect(result.current.isStreaming).toBe(false);
  });

  it("drops stale reasoning-only placeholders before sending the next user turn", () => {
    const fake = fakeClient();
    const initialMessages = [
      {
        id: "stale-thinking",
        role: "assistant" as const,
        content: "",
        reasoning: "leftover thinking",
        reasoningStreaming: false,
        createdAt: Date.now(),
      },
    ];
    const { result } = renderHook(
      () => useDurinStream("chat-stale-thinking", initialMessages),
      { wrapper: wrap(fake.client) },
    );

    act(() => {
      result.current.send("fine");
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].role).toBe("user");
    expect(result.current.messages[0].content).toBe("fine");
  });

  it("keys a complete assistant message by the server-assigned id", () => {
    // Command outputs carry a stable ``id`` (also persisted to the transcript).
    // The live append must adopt it so a later canonical refetch reconciles by
    // React key instead of duplicating/dropping the row.
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-id", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-id", {
        event: "message",
        chat_id: "chat-id",
        id: "msg-server-1",
        text: "## Persona",
        render_as: "text",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].id).toBe("msg-server-1");
    expect(result.current.messages[0].renderAs).toBe("text");
  });

  it("adopts the server id even when absorbing a reasoning placeholder", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-id2", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-id2", {
        event: "reasoning_delta",
        chat_id: "chat-id2",
        text: "thinking",
      });
      fake.emit("chat-id2", { event: "reasoning_end", chat_id: "chat-id2" });
      fake.emit("chat-id2", {
        event: "message",
        chat_id: "chat-id2",
        id: "msg-server-2",
        text: "final answer",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].id).toBe("msg-server-2");
    expect(result.current.messages[0].content).toBe("final answer");
  });

  it("falls back to a generated id when the server omits one", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-id3", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-id3", {
        event: "message",
        chat_id: "chat-id3",
        text: "no id here",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(typeof result.current.messages[0].id).toBe("string");
    expect(result.current.messages[0].id.length).toBeGreaterThan(0);
  });

  it("attaches assistant media_urls to complete messages", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-m", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-m", {
        event: "message",
        chat_id: "chat-m",
        text: "video ready",
        media_urls: [{ url: "/api/media/sig/payload", name: "demo.mp4" }],
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].media).toEqual([
      { kind: "video", url: "/api/media/sig/payload", name: "demo.mp4" },
    ]);
  });

  it("suppresses redundant stream confirmation after assistant media", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-img-result", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-img-result", {
        event: "message",
        chat_id: "chat-img-result",
        text: "image ready",
        media_urls: [{ url: "/api/media/sig/image", name: "generated.png" }],
      });
      fake.emit("chat-img-result", {
        event: "message",
        chat_id: "chat-img-result",
        text: "message()",
        kind: "tool_hint",
      });
      fake.emit("chat-img-result", {
        event: "delta",
        chat_id: "chat-img-result",
        text: "发送成功",
      });
      fake.emit("chat-img-result", {
        event: "stream_end",
        chat_id: "chat-img-result",
      });
      fake.emit("chat-img-result", {
        event: "turn_end",
        chat_id: "chat-img-result",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("image ready");
    expect(result.current.messages[0].media).toHaveLength(1);
  });

  it("stops the active turn without adding a user slash command bubble", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-stop", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      result.current.send("long task");
    });
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.isStreaming).toBe(true);

    act(() => {
      result.current.stop();
    });

    expect(fake.client.sendMessage).toHaveBeenLastCalledWith("chat-stop", "/stop");
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("long task");
  });

  it("keeps streaming alive across stream_end and completes on turn_end", () => {
    const fake = fakeClient();
    const onTurnEnd = vi.fn();
    const { result } = renderHook(() => useDurinStream("chat-s", EMPTY_MESSAGES, false, onTurnEnd), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-s", {
        event: "delta",
        chat_id: "chat-s",
        text: "Hello",
      });
    });

    expect(result.current.isStreaming).toBe(true);
    expect(result.current.messages[0]).toMatchObject({
      role: "assistant",
      content: "Hello",
      isStreaming: true,
    });

    act(() => {
      fake.emit("chat-s", {
        event: "stream_end",
        chat_id: "chat-s",
      });
    });

    expect(result.current.isStreaming).toBe(true);
    // The segment's row finalizes at stream_end (a later segment opens a new
    // bubble); the GLOBAL streaming flag stays up until turn_end.
    expect(result.current.messages[0].isStreaming).toBe(false);

    act(() => {
      fake.emit("chat-s", {
        event: "message",
        chat_id: "chat-s",
        text: "Hello world",
      });
    });

    expect(result.current.isStreaming).toBe(true);
    expect(result.current.messages.at(-1)).toMatchObject({
      role: "assistant",
      content: "Hello world",
    });

    act(() => {
      fake.emit("chat-s", {
        event: "turn_end",
        chat_id: "chat-s",
      });
    });

    expect(result.current.isStreaming).toBe(false);
    expect(result.current.messages.every((message) => !message.isStreaming)).toBe(true);
    expect(onTurnEnd).toHaveBeenCalledTimes(1);
  });

  it("stamps latency on the last assistant bubble from turn_end", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-lat", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-lat", {
        event: "delta",
        chat_id: "chat-lat",
        text: "Hi",
      });
    });

    act(() => {
      fake.emit("chat-lat", {
        event: "turn_end",
        chat_id: "chat-lat",
        latency_ms: 2400,
      });
    });

    const lastAssistant = [...result.current.messages].reverse().find((m) => m.role === "assistant");
    expect(lastAssistant?.latencyMs).toBe(2400);
  });

  it("tracks goal_status running and clears on idle", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useDurinStream("chat-g", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    expect(result.current.runStartedAt).toBeNull();

    act(() => {
      fake.emit("chat-g", {
        event: "goal_status",
        chat_id: "chat-g",
        status: "running",
        started_at: 1700,
      });
    });
    expect(result.current.runStartedAt).toBe(1700);

    act(() => {
      fake.emit("chat-g", {
        event: "goal_status",
        chat_id: "chat-g",
        status: "idle",
      });
    });
    expect(result.current.runStartedAt).toBeNull();
  });

  it("restores runStartedAt after switching away and back when goal_status was recorded without a subscriber", () => {
    const fake = fakeClient();
    const { result, rerender } = renderHook(
      ({ chatId }: { chatId: string }) => useDurinStream(chatId, EMPTY_MESSAGES),
      {
        wrapper: wrap(fake.client),
        initialProps: { chatId: "chat-a" },
      },
    );

    act(() => {
      fake.emit("chat-a", {
        event: "goal_status",
        chat_id: "chat-a",
        status: "running",
        started_at: 4242,
      });
    });
    expect(result.current.runStartedAt).toBe(4242);

    rerender({ chatId: "chat-b" });
    expect(result.current.runStartedAt).toBeNull();

    act(() => {
      fake.emit("chat-a", {
        event: "goal_status",
        chat_id: "chat-a",
        status: "running",
        started_at: 9001,
      });
    });

    rerender({ chatId: "chat-a" });
    expect(result.current.runStartedAt).toBe(9001);
  });

  it("tracks goal_state per chat and restores after switching sessions", () => {
    const fake = fakeClient();
    const { result, rerender } = renderHook(
      ({ chatId }: { chatId: string }) => useDurinStream(chatId, EMPTY_MESSAGES),
      {
        wrapper: wrap(fake.client),
        initialProps: { chatId: "chat-a" },
      },
    );

    act(() => {
      fake.emit("chat-a", {
        event: "goal_state",
        chat_id: "chat-a",
        goal_state: { active: true, ui_summary: "Alpha" },
      });
    });
    expect(result.current.goalState).toEqual({ active: true, ui_summary: "Alpha" });

    act(() => {
      fake.emit("chat-b", {
        event: "goal_state",
        chat_id: "chat-b",
        goal_state: { active: true, objective: "Beta task" },
      });
    });

    rerender({ chatId: "chat-b" });
    expect(result.current.goalState).toEqual({ active: true, objective: "Beta task" });

    rerender({ chatId: "chat-a" });
    expect(result.current.goalState).toEqual({ active: true, ui_summary: "Alpha" });

    act(() => {
      fake.emit("chat-a", {
        event: "goal_state",
        chat_id: "chat-a",
        goal_state: { active: false },
      });
    });
    expect(result.current.goalState).toEqual({ active: false });
  });

  it("send passes steer + clientMsgId on the wire and flags the local row", () => {
    const fake = fakeClient();
    const { result } = renderHook(
      () => useDurinStream("chat-s", EMPTY_MESSAGES),
      { wrapper: wrap(fake.client) },
    );

    act(() => {
      result.current.send("focus on tests", undefined, { steer: true });
    });

    expect(fake.client.sendMessage).toHaveBeenCalledWith(
      "chat-s",
      "focus on tests",
      undefined,
      expect.objectContaining({ steer: true, clientMsgId: expect.any(String) }),
    );
    const row = result.current.messages.at(-1)!;
    expect(row.steer).toBe(true);
    // The wire clientMsgId is the row id, so queued acks can target it.
    const opts = (fake.client.sendMessage as ReturnType<typeof vi.fn>).mock.calls[0][3];
    expect(opts.clientMsgId).toBe(row.id);
  });

  it("marks a row queued on message_queued and clears it on queued_consumed", () => {
    const fake = fakeClient();
    const { result } = renderHook(
      () => useDurinStream("chat-q", EMPTY_MESSAGES),
      { wrapper: wrap(fake.client) },
    );

    act(() => {
      result.current.send("later question");
    });
    const rowId = result.current.messages.at(-1)!.id;

    act(() => {
      fake.emit("chat-q", {
        event: "message_queued",
        chat_id: "chat-q",
        client_msg_id: rowId,
      });
    });
    expect(result.current.messages.at(-1)!.queued).toBe(true);

    act(() => {
      fake.emit("chat-q", {
        event: "queued_consumed",
        chat_id: "chat-q",
        client_msg_ids: [rowId],
      });
    });
    expect(result.current.messages.at(-1)!.queued).toBe(false);
  });

  it("clears stale queued flags on turn_end", () => {
    const fake = fakeClient();
    const { result } = renderHook(
      () => useDurinStream("chat-q2", EMPTY_MESSAGES),
      { wrapper: wrap(fake.client) },
    );

    act(() => {
      result.current.send("will be re-dispatched");
    });
    const rowId = result.current.messages.at(-1)!.id;
    act(() => {
      fake.emit("chat-q2", {
        event: "message_queued",
        chat_id: "chat-q2",
        client_msg_id: rowId,
      });
    });
    expect(result.current.messages.at(-1)!.queued).toBe(true);

    act(() => {
      fake.emit("chat-q2", { event: "turn_end", chat_id: "chat-q2" });
    });
    expect(result.current.messages.at(-1)!.queued).toBe(false);
  });

  it("renders post-stream_end segments as separate bubbles (deferred answers don't concatenate)", () => {
    const fake = fakeClient();
    const { result } = renderHook(
      () => useDurinStream("chat-seg", EMPTY_MESSAGES),
      { wrapper: wrap(fake.client) },
    );

    act(() => {
      fake.emit("chat-seg", { event: "delta", chat_id: "chat-seg", text: "TRABAJO TERMINADO" });
      fake.emit("chat-seg", { event: "stream_end", chat_id: "chat-seg" });
    });
    // The answer to a message queued mid-turn streams as a NEW segment.
    act(() => {
      fake.emit("chat-seg", { event: "delta", chat_id: "chat-seg", text: "4" });
      fake.emit("chat-seg", { event: "stream_end", chat_id: "chat-seg" });
      fake.emit("chat-seg", { event: "turn_end", chat_id: "chat-seg" });
    });

    const assistants = result.current.messages.filter((m) => m.role === "assistant");
    expect(assistants.map((m) => m.content)).toEqual(["TRABAJO TERMINADO", "4"]);
  });

  it("clears the steer chip on turn_end (matches replay, which renders no chips)", () => {
    const fake = fakeClient();
    const { result } = renderHook(
      () => useDurinStream("chat-s2", EMPTY_MESSAGES),
      { wrapper: wrap(fake.client) },
    );

    act(() => {
      result.current.send("adjust course", undefined, { steer: true });
    });
    expect(result.current.messages.at(-1)!.steer).toBe(true);

    act(() => {
      fake.emit("chat-s2", { event: "turn_end", chat_id: "chat-s2" });
    });
    expect(result.current.messages.at(-1)!.steer).toBe(false);
  });

});
