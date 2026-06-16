import { afterEach, describe, expect, it, vi } from "vitest";
import { listQuarantine, listSkills, saveSkill, searchSkills, setSkillMode } from "@/lib/api";

function mockFetchOnce(json: unknown) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(JSON.stringify(json), { status: 200, headers: { "content-type": "application/json" } }),
  );
}

afterEach(() => vi.restoreAllMocks());

describe("skills api", () => {
  it("listSkills hits /api/v1/skills and returns rows", async () => {
    const f = mockFetchOnce({ status: 200, data: { skills: [{ name: "a", source: "workspace", mode: "manual" }], store_head: null } });
    const rows = await listSkills("tok");
    expect(rows[0].name).toBe("a");
    expect(String(f.mock.calls[0][0])).toContain("/api/v1/skills");
  });

  it("setSkillMode POSTs to /api/v1/skills/{name}/mode with JSON body", async () => {
    const f = mockFetchOnce({ status: 200, data: { ok: true } });
    await setSkillMode("tok", "my skill", "auto");
    const url = String(f.mock.calls[0][0]);
    expect(url).toContain("/api/v1/skills/my%20skill/mode");
    const init = f.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toMatchObject({ name: "my skill", value: "auto" });
  });

  it("saveSkill POSTs content in the JSON body", async () => {
    const f = mockFetchOnce({ status: 200, data: { ok: true } });
    await saveSkill("tok", "a", "BODY");
    const url = String(f.mock.calls[0][0]);
    expect(url).toContain("/api/v1/skills/a/save");
    const init = f.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toMatchObject({ name: "a", content: "BODY" });
  });

  it("listSkills preserves the §8.C verdict and findings fields", async () => {
    mockFetchOnce({
      status: 200,
      data: {
        skills: [
          {
            name: "evil",
            source: "workspace",
            mode: "manual",
            status: "active",
            verdict: "dangerous",
            findings: [
              { category: "prompt_injection", severity: "dangerous", where: "SKILL.md", detail: "ignore-previous-instructions" },
            ],
          },
        ],
        store_head: null,
      },
    });
    const rows = await listSkills("tok");
    expect(rows[0].verdict).toBe("dangerous");
    expect(rows[0].findings?.[0].category).toBe("prompt_injection");
  });

  it("searchSkills encodes the query and returns hits", async () => {
    const f = mockFetchOnce({
      status: 200,
      data: {
        hits: [
          { name: "pdf", ref: "github:acme/pdf", registry: "acme", description: "d", signals: { installs: 3 } },
        ],
      },
    });
    const res = await searchSkills("tok", "p df");
    expect(res.hits[0].ref).toBe("github:acme/pdf");
    expect(res.hits[0].signals.installs).toBe(3);
    const url = String(f.mock.calls[0][0]);
    expect(url).toContain("/api/v1/skills/search");
    expect(url).toContain("q=p+df");
    expect(url).toContain("limit=0");
  });

  it("listQuarantine hits /api/v1/skills/quarantine and returns rows", async () => {
    const f = mockFetchOnce({
      status: 200,
      data: {
        quarantined: [
          { name: "q", status: "quarantined", source: "github:owner/repo", verdict: "caution", findings: [] },
        ],
      },
    });
    const rows = await listQuarantine("tok");
    expect(rows[0].name).toBe("q");
    expect(rows[0].source).toBe("github:owner/repo");
    expect(String(f.mock.calls[0][0])).toContain("/api/v1/skills/quarantine");
  });
});
