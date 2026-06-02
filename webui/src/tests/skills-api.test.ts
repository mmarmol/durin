import { afterEach, describe, expect, it, vi } from "vitest";
import { listSkills, saveSkill, setSkillMode } from "@/lib/api";

function mockFetchOnce(json: unknown) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(JSON.stringify(json), { status: 200, headers: { "content-type": "application/json" } }),
  );
}

afterEach(() => vi.restoreAllMocks());

describe("skills api", () => {
  it("listSkills hits /api/skills and returns rows", async () => {
    const f = mockFetchOnce({ skills: [{ name: "a", source: "workspace", mode: "manual" }], store_head: null });
    const rows = await listSkills("tok");
    expect(rows[0].name).toBe("a");
    expect(String(f.mock.calls[0][0])).toContain("/api/skills");
  });

  it("setSkillMode encodes name and value", async () => {
    const f = mockFetchOnce({ ok: true });
    await setSkillMode("tok", "my skill", "auto");
    const url = String(f.mock.calls[0][0]);
    expect(url).toContain("/api/skills/my%20skill/mode");
    expect(url).toContain("value=auto");
  });

  it("saveSkill puts content in the query", async () => {
    const f = mockFetchOnce({ ok: true });
    await saveSkill("tok", "a", "BODY");
    const url = String(f.mock.calls[0][0]);
    expect(url).toContain("/api/skills/a/save");
    expect(url).toContain("content=BODY");
  });
});
