/**
 * Read-only affordance for non-websocket sessions.
 *
 * When the active session comes from a non-websocket channel (Telegram, CLI,
 * subagent…) the composer textarea must be disabled and a read-only banner
 * must be visible.
 */
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThreadShell } from "@/components/thread/ThreadShell";
import { ClientProvider } from "@/providers/ClientProvider";
import type { ChatSummary } from "@/lib/types";

function makeClient() {
  return {
    status: "open" as const,
    defaultChatId: null as string | null,
    onStatus: () => () => {},
    onRuntimeModelUpdate: () => () => {},
    getRunStartedAt: () => null,
    getGoalState: () => undefined,
    onChat: () => () => {},
    onError: () => () => {},
    onSessionUpdate: () => () => {},
    sendMessage: vi.fn(),
    newChat: vi.fn(),
    attach: vi.fn(),
    connect: vi.fn(),
    close: vi.fn(),
    updateUrl: vi.fn(),
    storeSecret: vi.fn(),
  };
}

function wrap(client: ReturnType<typeof makeClient>, children: ReactNode) {
  return (
    <ClientProvider
      client={client as unknown as import("@/lib/durin-client").DurinClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

function telegramSession(chatId: string): ChatSummary {
  return {
    key: `telegram:${chatId}`,
    channel: "telegram",
    chatId,
    createdAt: null,
    updatedAt: null,
    preview: "hi from telegram",
  };
}

function websocketSession(chatId: string): ChatSummary {
  return {
    key: `websocket:${chatId}`,
    channel: "websocket",
    chatId,
    createdAt: null,
    updatedAt: null,
    preview: "hi from web",
  };
}

describe("read-only channel affordance", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({}),
      }),
    );
  });

  it("disables the composer and shows a read-only banner for telegram sessions", async () => {
    const client = makeClient();
    render(
      wrap(
        client,
        <ThreadShell
          session={telegramSession("tg-chat-1")}
          title="Telegram chat"
          onToggleSidebar={() => {}}
        />,
      ),
    );

    // Composer input must be disabled
    const textarea = screen.getByLabelText("Message input");
    expect(textarea).toBeDisabled();

    // A read-only banner must be present
    const banner = screen.getByRole("status");
    expect(banner).toBeInTheDocument();
    // Banner contains the channel name
    expect(banner.textContent?.toLowerCase()).toMatch(/telegram/i);
  });

  it("does NOT disable the composer for websocket sessions", async () => {
    const client = makeClient();
    render(
      wrap(
        client,
        <ThreadShell
          session={websocketSession("ws-chat-1")}
          title="Web chat"
          onToggleSidebar={() => {}}
        />,
      ),
    );

    const textarea = screen.getByLabelText("Message input");
    // websocket sessions: chatId is available → disabled={!chatId} resolves to false
    // (the ThreadShell's existing disabled logic already handles this)
    expect(textarea).not.toBeDisabled();

    // No read-only banner
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
