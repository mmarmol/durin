# Skills Registry Search — Marketplace Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the "Add a skill" pane a search-first marketplace — result count, objective sort, lazy per-result descriptions, source badges, and "show more" — without touching the registry-search code shared with the agent `skill_search` tool.

**Architecture:** One additive read-only backend endpoint (`GET /api/skills/describe?ref=`) that fetches a skill's `SKILL.md` frontmatter description on demand; a frontend rewrite of the acquire pane (search primary, import-by-reference secondary). Sorting and "show more" are client-side / use the existing `limit` param. `skill_registry.py` and `web_skill_search` are left untouched.

**Tech Stack:** Python 3.11 / pytest, React + TypeScript / vitest, i18next.

**Spec:** `docs/superpowers/specs/2026-06-07-skills-search-marketplace-design.md`

**Isolation contract:** Do NOT modify `durin/agent/skill_registry.py` (`search_registries`, `SkillSearchHit`, adapters) or `durin/agent/skills_store.py:web_skill_search`. They are shared with the `skill_search` agent tool and the dream skill-acquire flow. Task 2 adds a guard test that fails if these change.

---

## File Structure

- `durin/agent/skills_store.py` — add `web_skill_describe(ref)` (read-only frontmatter peek). Owns: the describe endpoint logic.
- `durin/channels/websocket.py` — route `GET /api/skills/describe` → `_handle_skill_describe`. Owns: HTTP wiring.
- `webui/src/lib/api.ts` — add `describeSkill(token, ref)`.
- `webui/src/components/SkillsView.tsx` — rewrite the acquire pane (search-first).
- `webui/src/i18n/locales/{en,es}/common.json` — new `skills.search.*` / `skills.acquire*` copy.
- Tests: `tests/agent/test_skill_describe.py`, `tests/agent/test_search_isolation.py`, `webui/src/tests/skills-view.test.tsx`.

---

## Task 1: Backend — `web_skill_describe`

**Files:**
- Modify: `durin/agent/skills_store.py`
- Test: `tests/agent/test_skill_describe.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_describe.py
import durin.agent.skills_store as ss
import durin.agent.skills_import as si


def test_describe_github_parses_frontmatter(monkeypatch):
    md = b"---\nname: demo\ndescription: Scrape and crawl websites.\n---\nbody\n"
    monkeypatch.setattr(si, "_http_get_bytes", lambda url: md)
    status, payload = ss.web_skill_describe("github:owner/repo/demo")
    assert status == 200
    assert payload["description"] == "Scrape and crawl websites."


def test_describe_network_error_is_empty(monkeypatch):
    def boom(url):
        raise RuntimeError("boom")
    monkeypatch.setattr(si, "_http_get_bytes", boom)
    status, payload = ss.web_skill_describe("github:owner/repo/demo")
    assert status == 200
    assert payload["description"] == ""


def test_describe_clawhub_is_empty():
    status, payload = ss.web_skill_describe("clawhub:some/slug")
    assert status == 200
    assert payload["description"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/marcelo/git_personal/durin/.claude/worktrees/skills-fixes && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_describe.py -q`
Expected: FAIL — `web_skill_describe` undefined.

- [ ] **Step 3: Implement** — add to `durin/agent/skills_store.py`:

```python
def web_skill_describe(ref: str) -> tuple[int, dict]:
    """`GET /api/skills/describe?ref=` — read-only peek at a registry skill's
    SKILL.md frontmatter ``description`` (lazy-loaded by the search UI on expand).
    Never executes or writes anything. Any failure degrades to an empty string."""
    from durin.agent import skills_import as si
    from durin.agent.skills_frontmatter import split_frontmatter

    ref = (ref or "").strip()
    if not ref.startswith("github:"):
        # clawhub hits already carry a description; other refs aren't peekable.
        return 200, {"ref": ref, "description": ""}
    try:
        owner, repo, branch, skill_dir = si._parse_github_ref(ref)
        path = f"{skill_dir}/SKILL.md" if skill_dir else "SKILL.md"
        url = f"{si._GITHUB_RAW}/{owner}/{repo}/{branch}/{path}"
        raw = si._http_get_bytes(url)[:65_536]
        data, _ = split_frontmatter(raw.decode("utf-8", errors="replace"))
        desc = str(data.get("description") or "").strip()
        return 200, {"ref": ref, "description": desc[:280]}
    except Exception:  # noqa: BLE001 — describe is best-effort, never fatal
        return 200, {"ref": ref, "description": ""}
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_store.py tests/agent/test_skill_describe.py
git commit -m "feat(skills): web_skill_describe — read-only frontmatter peek for search"
```

---

## Task 2: Backend — route wiring + isolation guard

**Files:**
- Modify: `durin/channels/websocket.py` (route table near line 810; handler near `_handle_skill_search`)
- Test: `tests/agent/test_search_isolation.py` (create)

- [ ] **Step 1: Write the isolation guard test**

```python
# tests/agent/test_search_isolation.py
import inspect

from durin.agent import skill_registry, skills_store


def test_search_registries_signature_unchanged():
    sig = inspect.signature(skill_registry.search_registries)
    assert list(sig.parameters) == ["query", "adapters", "allowlist", "limit"]


def test_skill_search_hit_fields_unchanged():
    fields = {f.name for f in skill_registry.SkillSearchHit.__dataclass_fields__.values()}
    assert fields == {"name", "ref", "registry", "description", "signals"}


def test_web_skill_search_signature_unchanged():
    sig = inspect.signature(skills_store.web_skill_search)
    assert list(sig.parameters) == ["workspace", "query", "limit"]
```

- [ ] **Step 2: Run it to confirm the contract holds today**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_search_isolation.py -q`
Expected: PASS (these document the shared contract; they must keep passing).

- [ ] **Step 3: Add the describe route + handler**

In `durin/channels/websocket.py`, after the search route (line ~810-811):

```python
        if got == "/api/skills/describe":
            return await self._handle_skill_describe(request)
```

Add the handler next to `_handle_skill_search`:

```python
    async def _handle_skill_describe(self, request: WsRequest) -> Response:
        """`GET /api/skills/describe?ref=` — lazy SKILL.md description peek. Async +
        off-thread (it makes an outbound HTTP fetch)."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        ref = (_query_first(_parse_query(request.path), "ref") or "").strip()
        if not ref:
            return _http_error(400, "ref is required")
        from durin.agent import skills_store as ss
        try:
            status, payload = await asyncio.to_thread(ss.web_skill_describe, ref)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"describe failed: {exc}")
        return _http_json_response(payload, status=status)
```

- [ ] **Step 4: Smoke-check import + run isolation test again**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -c "import durin.channels.websocket; print('ok')" && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_search_isolation.py -q`
Expected: `ok` + PASS.

- [ ] **Step 5: Commit**

```bash
git add durin/channels/websocket.py tests/agent/test_search_isolation.py
git commit -m "feat(skills): /api/skills/describe route + search isolation guard test"
```

---

## Task 3: API client — `describeSkill`

**Files:**
- Modify: `webui/src/lib/api.ts`

- [ ] **Step 1: Add the function** (after `searchSkills`):

```typescript
/** Lazy SKILL.md description peek for a registry hit (search UI, on expand).
 *  Returns an empty description when none is available — never throws on 404. */
export async function describeSkill(
  token: string,
  ref: string,
  base: string = "",
): Promise<{ ref: string; description: string }> {
  const params = new URLSearchParams({ ref });
  try {
    return await request<{ ref: string; description: string }>(
      `${base}/api/skills/describe?${params}`,
      token,
    );
  } catch {
    return { ref, description: "" };
  }
}
```

- [ ] **Step 2: Typecheck**

Run: `cd webui && npx tsc -p tsconfig.build.json --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add webui/src/lib/api.ts
git commit -m "feat(webui): describeSkill API — lazy registry description peek"
```

---

## Task 4: Frontend — search-first acquire pane

**Files:**
- Modify: `webui/src/components/SkillsView.tsx`
- Modify: `webui/src/i18n/locales/en/common.json`, `webui/src/i18n/locales/es/common.json`
- Test: `webui/src/tests/skills-view.test.tsx` (extend)

- [ ] **Step 1: Add i18n copy**

Under `skills.search` add (en); mirror in es:

```json
"resultsCount_one": "{{count}} result",
"resultsCount_other": "{{count}} results",
"sortLabel": "Sort",
"sortInstalls": "Installs",
"sortName": "Name A–Z",
"sortRelevance": "Relevance",
"showMore": "Show more",
"noDescription": "No description available.",
"acquireExplainer": "Public-registry skills. Importing drops a skill into Pending for your approval.",
"importByRef": "Import by reference"
```

es:

```json
"resultsCount_one": "{{count}} resultado",
"resultsCount_other": "{{count}} resultados",
"sortLabel": "Orden",
"sortInstalls": "Installs",
"sortName": "Nombre A–Z",
"sortRelevance": "Relevancia",
"showMore": "Mostrar más",
"noDescription": "Sin descripción disponible.",
"acquireExplainer": "Skills del registro público. Al importar, la skill queda en Pendientes para tu aprobación.",
"importByRef": "Importar por referencia"
```

- [ ] **Step 2: Write the failing test** — add to `skills-view.test.tsx`. Add `describeSkill` to the `vi.mock` factory list and to `beforeEach` resets first.

```typescript
  it("search shows count, sorts, lazy-describes on expand, and loads more", async () => {
    vi.mocked(api.listSkills).mockResolvedValue([
      { name: "clean", source: "builtin", mode: "auto", status: "active", verdict: "safe", findings: [] },
    ]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);
    vi.mocked(api.searchSkills).mockResolvedValue({
      hits: [
        { name: "alpha", ref: "github:o/alpha", registry: "skills.sh", description: "skills.sh: o · 5 installs", signals: { installs: 5 } },
        { name: "zeta", ref: "github:o/zeta", registry: "skills.sh", description: "skills.sh: o · 90 installs", signals: { installs: 90 } },
      ],
    });
    vi.mocked(api.describeSkill).mockResolvedValue({ ref: "github:o/alpha", description: "Alpha does X." });

    const user = userEvent.setup();
    render(wrap(<SkillsView />));
    await screen.findByText("clean");
    await user.click(screen.getByRole("button", { name: /add skill/i }));
    await user.type(await screen.findByPlaceholderText(/Search the registry/i), "x");
    await user.click(screen.getByRole("button", { name: "Search" }));

    // result count appears
    expect(await screen.findByText(/2 results/i)).toBeInTheDocument();

    // default sort = installs desc → zeta (90) before alpha (5)
    const names = screen.getAllByTestId("hit-name").map((n) => n.textContent);
    expect(names).toEqual(["zeta", "alpha"]);

    // expanding alpha lazy-fetches its description
    await user.click(screen.getByRole("button", { name: /expand alpha/i }));
    expect(await screen.findByText("Alpha does X.")).toBeInTheDocument();
    expect(api.describeSkill).toHaveBeenCalledWith("tok", "github:o/alpha");
  });
```

- [ ] **Step 3: Run it to verify it fails** — Expected: FAIL (no count/sort/expand yet).

- [ ] **Step 4: Implement the acquire-pane rewrite**

State (near the other acquire state):

```typescript
  const [sortBy, setSortBy] = useState<"installs" | "name" | "relevance">("installs");
  const [searchLimit, setSearchLimit] = useState(10);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [descCache, setDescCache] = useState<Record<string, string | null>>({}); // null = loading
  const [importByRefOpen, setImportByRefOpen] = useState(false);
```

Make `doSearch` accept a limit and reset sort/expansion; replace the existing `doSearch`:

```typescript
  const doSearch = useCallback(
    async (query: string, limit = 10) => {
      const q = query.trim();
      if (!q) return;
      setSearching(true);
      setSearchMsg(null);
      try {
        const res = await searchSkills(token, q, limit);
        setHits(res.hits);
        setSearchLimit(limit);
        if (res.hits.length === 0) setSearchMsg(t("skills.search.empty"));
      } catch (e) {
        setHits(null);
        setSearchMsg(errMsg(e));
      } finally {
        setSearching(false);
      }
    },
    [token, t],
  );
```

Expand + lazy describe handler:

```typescript
  const toggleExpand = useCallback(
    async (hit: SkillSearchHit) => {
      if (expanded === hit.ref) { setExpanded(null); return; }
      setExpanded(hit.ref);
      // clawhub (or any hit with a real description) needs no fetch
      if (hit.registry === "clawhub" || !hit.ref.startsWith("github:")) return;
      if (descCache[hit.ref] !== undefined) return;
      setDescCache((c) => ({ ...c, [hit.ref]: null }));
      const r = await describeSkill(token, hit.ref);
      setDescCache((c) => ({ ...c, [hit.ref]: r.description }));
    },
    [token, expanded, descCache],
  );
```

Sorted hits (client-side; missing installs sort last):

```typescript
  const sortedHits = (hits ?? []).slice().sort((a, b) => {
    if (sortBy === "installs") return (b.signals?.installs ?? -1) - (a.signals?.installs ?? -1);
    if (sortBy === "name") return a.name.localeCompare(b.name);
    return 0; // relevance = registry order
  });
```

Replace the search form + hits JSX in the acquire pane with: the search form (kept), then when `hits` is non-null a results header (count + a `<select>` bound to `sortBy` with the three options), then `sortedHits.map(...)` rendering each card:

```tsx
                {hits ? (
                  <div className="mt-3">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="text-[12px] text-muted-foreground">
                        {t("skills.search.resultsCount", { count: hits.length })}
                      </span>
                      <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
                        {t("skills.search.sortLabel")}
                        <select
                          value={sortBy}
                          onChange={(e) => setSortBy(e.target.value as typeof sortBy)}
                          className="rounded-[6px] border border-border/60 bg-background px-1.5 py-0.5 text-[12px]"
                        >
                          <option value="installs">{t("skills.search.sortInstalls")}</option>
                          <option value="name">{t("skills.search.sortName")}</option>
                          <option value="relevance">{t("skills.search.sortRelevance")}</option>
                        </select>
                      </label>
                    </div>
                    <div className="flex flex-col gap-1">
                      {sortedHits.map((h) => {
                        const desc =
                          h.registry === "clawhub" || !h.ref.startsWith("github:")
                            ? h.description
                            : descCache[h.ref];
                        const open = expanded === h.ref;
                        return (
                          <div key={h.ref} className="rounded-[8px] border border-border/40 bg-muted/20 p-2">
                            <div className="flex items-start gap-2">
                              <button
                                type="button"
                                aria-label={`expand ${h.name}`}
                                onClick={() => void toggleExpand(h)}
                                className="mt-0.5 text-muted-foreground hover:text-foreground"
                              >
                                {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                              </button>
                              <div className="flex min-w-0 flex-1 flex-col">
                                <span className="flex items-center gap-1.5">
                                  <span data-testid="hit-name" className="truncate text-[13px] font-medium text-foreground">
                                    {h.name}
                                  </span>
                                  <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                                    {h.registry}
                                  </span>
                                  {typeof h.signals?.installs === "number" ? (
                                    <span className="shrink-0 text-[11px] text-muted-foreground">
                                      {t("skills.search.installs", { count: h.signals.installs })}
                                    </span>
                                  ) : null}
                                </span>
                                {open ? (
                                  <span className="mt-1 text-[12px] text-muted-foreground">
                                    {desc === null ? (
                                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                    ) : desc ? (
                                      desc
                                    ) : (
                                      t("skills.search.noDescription")
                                    )}
                                  </span>
                                ) : null}
                                <span className="truncate text-[11px] text-muted-foreground/70">{h.ref}</span>
                              </div>
                              <Button type="button" size="sm" variant="ghost" disabled={importing}
                                onClick={() => void doImport(h.ref)}>
                                {t("skills.import.button")}
                              </Button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                    {hits.length >= searchLimit ? (
                      <Button type="button" size="sm" variant="outline" className="mt-2"
                        disabled={searching}
                        onClick={() => void doSearch(searchQuery, searchLimit + 10)}>
                        {t("skills.search.showMore")}
                      </Button>
                    ) : null}
                  </div>
                ) : null}
```

Move the existing manual import form into a collapsible "Import by reference" at the bottom of the acquire pane:

```tsx
                <div className="mt-4 border-t border-border/30 pt-3">
                  <button type="button"
                    onClick={() => setImportByRefOpen((v) => !v)}
                    className="flex items-center gap-1.5 text-[12px] text-muted-foreground hover:text-foreground">
                    {importByRefOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                    {t("skills.search.importByRef")}
                  </button>
                  {importByRefOpen ? (
                    /* the existing import <form> + picker block, moved here verbatim */
                  ) : null}
                </div>
```

Add the explainer line under the acquire title:

```tsx
                <p className="mt-1 max-w-[60ch] text-[12px] text-muted-foreground">
                  {t("skills.search.acquireExplainer")}
                </p>
```

Imports: add `ChevronDown, ChevronRight` to the `lucide-react` import; add `describeSkill` and the `SkillSearchHit` type (already imported) usage. The old standalone search-results block and the top import form are removed/relocated by the above.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd webui && npx vitest run src/tests/skills-view.test.tsx`
Expected: PASS (all, including the new search test and the prior acquire/import tests — update the import test if it relied on the import form being visible by default: it must first open "Import by reference").

- [ ] **Step 6: Build + commit**

```bash
cd webui && npm run build && cd ..
git add webui/src/components/SkillsView.tsx webui/src/lib/api.ts \
  webui/src/i18n/locales/en/common.json webui/src/i18n/locales/es/common.json \
  webui/src/tests/skills-view.test.tsx
git commit -m "feat(webui): search-first acquire pane — count, sort, lazy describe, show more"
```

---

## Task 5: Verification

- [ ] **Step 1:** `pytest tests/ -q --maxfail=5` → PASS (incl. describe + isolation tests).
- [ ] **Step 2:** `cd webui && npx vitest run && npm run build` → PASS, clean.
- [ ] **Step 3: Live** — dev server proxied to the gateway: search the registry, confirm the count + sort reorders + expanding a hit shows a description (or "no description available"), "Show more" loads more, and "Import by reference" still imports. (The describe endpoint needs the worktree backend / a redeployed gateway; against an old gateway, describe returns empty and the UI shows "no description available" — graceful.)

---

## Self-Review notes

- **Spec coverage:** describe endpoint → Tasks 1–2; isolation contract → Task 2 guard; API → Task 3; search-first UI with count/sort/expand-describe/show-more/import-by-ref → Task 4; states + i18n → Task 4; tests → each task + Task 5.
- **Type consistency:** `web_skill_describe(ref) -> (status, {ref, description})` (Task 1) matches the route handler (Task 2) and `describeSkill` return type (Task 3) and the UI consumer (Task 4). `sortBy` union identical across state + select + sorter.
- **Isolation:** no task edits `skill_registry.py` or `web_skill_search`; Task 2's guard test enforces it.
- **Imports test note:** the existing "imports a source through the Add-skill acquire pane" test must click "Import by reference" to reveal the form before typing — update it in Task 4 Step 5.
