import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchSkillSuggestions, acceptSkillSuggestion } from "./api";

afterEach(() => vi.restoreAllMocks());

describe("skill suggestions api", () => {
  it("unwraps the suggestions array", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ suggestions: [{ id: "a", skill: "x", type: "evolve", reason: "r", patch: null, created_at: "" }] }),
        { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    const out = await fetchSkillSuggestions("tok");
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe("a");
  });

  it("posts to the accept endpoint", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    await acceptSkillSuggestion("tok", "a");
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/skills/suggestions/a/accept"),
      expect.objectContaining({ method: "POST" }),
    );
  });
});
