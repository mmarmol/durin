import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteSession,
  disconnectCodex,
  fetchModelPicker,
  fetchSettings,
  fetchWebuiThread,
  listSessions,
  listSlashCommands,
  setApiReauthHandler,
  startCodexLoopbackAuth,
  updateProviderSettings,
  updateSettings,
  updateWebSearchSettings,
} from "@/lib/api";

describe("webui API helpers", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          deleted: true,
          key: "websocket:chat-1",
          messages: [],
          title: "",
          authorize_url: "",
          connected: false,
        }),
      }),
    );
  });

  it("percent-encodes websocket keys when fetching webui-thread snapshot", async () => {
    await fetchWebuiThread("tok", "websocket:chat-1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/sessions/websocket%3Achat-1/webui-thread",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
        credentials: "same-origin",
      }),
    );
  });

  it("percent-encodes websocket keys when deleting a session", async () => {
    await deleteSession("tok", "websocket:chat-1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/sessions/websocket%3Achat-1",
      expect.objectContaining({
        method: "DELETE",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
      }),
    );
  });

  it("serializes settings updates as a JSON POST body", async () => {
    await updateSettings("tok", {
      model: "openrouter/test",
      provider: "openrouter",
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/settings",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        body: JSON.stringify({ model: "openrouter/test", provider: "openrouter" }),
      }),
    );
  });

  it("serializes provider settings updates as a JSON POST body", async () => {
    await updateProviderSettings("tok", {
      provider: "openrouter",
      apiKey: "sk-or-test",
      apiBase: "https://openrouter.ai/api/v1",
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/settings/provider",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        body: JSON.stringify({ provider: "openrouter", apiKey: "sk-or-test", apiBase: "https://openrouter.ai/api/v1" }),
      }),
    );
  });

  it("serializes web search settings updates as a JSON POST body", async () => {
    await updateWebSearchSettings("tok", {
      provider: "searxng",
      baseUrl: "https://search.example.com",
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/settings/web-search",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        body: JSON.stringify({ provider: "searxng", baseUrl: "https://search.example.com" }),
      }),
    );
  });

  it("maps generated session titles from the sessions list", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        sessions: [
          {
            key: "websocket:chat-1",
            created_at: "2026-05-01T10:00:00",
            updated_at: "2026-05-01T10:01:00",
            title: "优化 WebUI 标题",
          },
        ],
      }),
    } as Response);

    await expect(listSessions("tok")).resolves.toMatchObject([
      {
        key: "websocket:chat-1",
        title: "优化 WebUI 标题",
        preview: "",
      },
    ]);
  });

  it("maps slash command metadata from the commands endpoint", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        commands: [
          {
            command: "/stop",
            title: "Stop current task",
            description: "Cancel the active task.",
            icon: "square",
          },
          {
            command: "/restart",
            title: "Restart durin",
            description: "Restart the bot process.",
            icon: "rotate-cw",
          },
          {
            command: "/history",
            title: "Show conversation history",
            description: "Print the last N messages.",
            icon: "history",
            arg_hint: "[n]",
          },
        ],
      }),
    } as Response);

    await expect(listSlashCommands("tok")).resolves.toEqual([
      {
        command: "/history",
        title: "Show conversation history",
        description: "Print the last N messages.",
        icon: "history",
        argHint: "[n]",
      },
    ]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/commands",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("uses POST for startCodexLoopbackAuth and DELETE for disconnectCodex (same-origin, not WS-constrained)", async () => {
    await startCodexLoopbackAuth("tok");
    await disconnectCodex("tok");
    const calls = vi.mocked(fetch).mock.calls;
    expect(calls[0][0]).toBe("/api/v1/oauth/codex/start-loopback");
    expect((calls[0][1] as RequestInit).method).toBe("POST");
    expect(calls[1][0]).toBe("/api/v1/oauth/codex");
    expect((calls[1][1] as RequestInit).method).toBe("DELETE");
  });

  it("fetches model picker entries with recents and returns them", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        entries: [
          { name: "base-model", provider: "openai_codex", group: "Easy pick", role: "default", ref: "default" },
          { name: "gemini-2.5-pro", provider: "gemini", group: "gemini", role: "catalog", ref: "gemini gemini-2.5-pro" },
        ],
      }),
    } as Response);

    const out = await fetchModelPicker("tok", ["gpt-5"]);

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/model/picker?recent=gpt-5",
      expect.objectContaining({ headers: { Authorization: "Bearer tok" } }),
    );
    expect(out[0].ref).toBe("default");
    expect(out[1].provider).toBe("gemini");
  });

  it("re-bootstraps and retries once when a request gets 401", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 401 } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ agent: { model: "glm-5.1" } }),
      } as Response);
    vi.stubGlobal("fetch", fetchMock);
    setApiReauthHandler(async () => "fresh-token");

    await fetchSettings("stale-token");

    // First call used the stale token, the retry used the fresh one.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer stale-token");
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe("Bearer fresh-token");
    setApiReauthHandler(null);
  });

  it("does not loop when the retry also 401s", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: false, status: 401 } as Response);
    vi.stubGlobal("fetch", fetchMock);
    setApiReauthHandler(async () => "fresh-token");

    await expect(fetchSettings("stale-token")).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(2); // original + one retry, no loop
    setApiReauthHandler(null);
  });
});
