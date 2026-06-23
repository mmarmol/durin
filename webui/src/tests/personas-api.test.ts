import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteSoul,
  deletePersona,
  listPersonas,
  listSouls,
  savePersona,
  saveSoul,
  setDefaultPersona,
  testPersona,
} from "@/lib/api";

const MOCK_SOUL = { slug: "default", body: "You are a helpful assistant." };
const MOCK_PERSONA = { name: "assistant", soul: "default", model: null, description: null, builtin: false };

describe("souls API helpers", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ souls: [MOCK_SOUL], soul: MOCK_SOUL, ok: true }),
      }),
    );
  });

  it("GETs /api/v1/souls and returns the souls array", async () => {
    const result = await listSouls("tok");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/souls",
      expect.objectContaining({ headers: { Authorization: "Bearer tok" } }),
    );
    expect(result).toEqual([MOCK_SOUL]);
  });

  it("POSTs to /api/v1/souls and returns the saved soul", async () => {
    const result = await saveSoul("tok", { slug: "default", body: "You are a helpful assistant." });

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/souls",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        body: JSON.stringify({ slug: "default", body: "You are a helpful assistant." }),
      }),
    );
    expect(result).toEqual(MOCK_SOUL);
  });

  it("DELETEs /api/v1/souls with a body containing the slug", async () => {
    await deleteSoul("tok", "default");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/souls",
      expect.objectContaining({
        method: "DELETE",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        body: JSON.stringify({ slug: "default" }),
      }),
    );
  });
});

describe("personas API helpers", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          personas: [MOCK_PERSONA],
          persona: MOCK_PERSONA,
          default: null,
          ok: true,
        }),
      }),
    );
  });

  it("GETs /api/v1/personas and returns personas + default", async () => {
    const result = await listPersonas("tok");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/personas",
      expect.objectContaining({ headers: { Authorization: "Bearer tok" } }),
    );
    expect(result.personas).toEqual([MOCK_PERSONA]);
    expect(result.default).toBeNull();
  });

  it("POSTs to /api/v1/personas and returns the saved persona", async () => {
    const result = await savePersona("tok", {
      name: "assistant",
      soul: "default",
      model: null,
      description: null,
    });

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/personas",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        body: JSON.stringify({
          name: "assistant",
          soul: "default",
          model: null,
          description: null,
        }),
      }),
    );
    expect(result).toEqual(MOCK_PERSONA);
  });

  it("DELETEs /api/v1/personas with a body containing the name", async () => {
    await deletePersona("tok", "assistant");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/personas",
      expect.objectContaining({
        method: "DELETE",
        body: JSON.stringify({ name: "assistant" }),
      }),
    );
  });

  it("POSTs to /api/v1/personas/default to set the default persona", async () => {
    await setDefaultPersona("tok", "assistant");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/personas/default",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "assistant" }),
      }),
    );
  });

  it("POSTs null to /api/v1/personas/default to clear the default", async () => {
    await setDefaultPersona("tok", null);

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/personas/default",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: null }),
      }),
    );
  });
});

describe("testPersona API helper", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ ok: true, reply: "Hello, I'm your assistant.", model: "openai gpt-4o", error: null }),
      }),
    );
  });

  it("POSTs to /api/v1/personas/test and returns the result", async () => {
    const result = await testPersona("tok", { model: "openai gpt-4o", soul: "default" });

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/personas/test",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
        body: JSON.stringify({ model: "openai gpt-4o", soul: "default" }),
      }),
    );
    expect(result.ok).toBe(true);
    expect(result.reply).toBe("Hello, I'm your assistant.");
  });

  it("sends null model and soul when not specified", async () => {
    await testPersona("tok", { model: null, soul: null });

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/personas/test",
      expect.objectContaining({
        body: JSON.stringify({ model: null, soul: null }),
      }),
    );
  });
});
