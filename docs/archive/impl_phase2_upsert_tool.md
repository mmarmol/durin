# Phase 2 — `memory_upsert_entity` tool

> Builds on Phase 1 (`memory_writer`). Executed on branch `memory-redesign-phase1`.

**Goal:** Give the agent a first-class tool to author/update an entity directly
(design §2.2/§2.4, decision b): the agent provides `name + aliases + relations +
body` (NOT attributes — dream owns those); the entity exists immediately via
`memory_writer`.

**Architecture:** `MemoryUpsertEntityTool` (auto-discovered by `loader.py`)
parses args → `FieldPatch`es → `write_entity(...)` under
`author_scope("agent_created")` (which the writer bridges to field-author
`agent`). Merge semantics, dangling relations allowed, dedup deferred to dream.

**Verified infra:** `Tool` ABC (`durin/agent/tools/base.py`): `name`,
`description`, `parameters` (via `@tool_parameters`), `create(cls, ctx)`,
`execute`, `to_schema`. `create` reads `ctx.workspace` + `ctx.app_config`
(memory_store template). The loader auto-discovers tool modules via
`pkgutil.iter_modules` → **no manual registration list**.

---

## Task 1: extend `write_entity` with a `name` kwarg (non-breaking)

`FieldPatch` has no `name` kind; the page's display name is set by the author,
not precedence-arbitrated. Add an optional `name` to `write_entity`.

**Files:** Modify `durin/memory/memory_writer.py`; Test `tests/memory/test_memory_writer.py`

- [ ] **Test**
```python
def test_write_entity_sets_display_name(tmp_path):
    write_entity(tmp_path, "company:mxhero",
                 [FieldPatch(kind="body_append", value="x", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO Inc.")
    page = EntityPage.from_file(_page_path(tmp_path, "company:mxhero"))
    assert page.name == "mxHERO Inc."
```
- [ ] **Impl**: add `name: str | None = None` param; after constructing/loading
  `page`, `if name: page.name = name` (before applying patches). Keep the
  create-default `name=slug` when `name` is None and the page is new.
- [ ] **Run + commit**: `feat(memory): write_entity accepts display name`

---

## Task 2: the tool module

**Files:** Create `durin/agent/tools/memory_upsert_entity.py`; Test `tests/agent/tools/test_memory_upsert_entity.py`

Params (`tool_parameters_schema`): `ref` (required, `<type>:<slug>`), `name`
(string), `aliases` (array of string), `relations` (array of object
`{to, type, ...}`), `body` (string). **No `attributes`** (decision b).

Description (the text the LLM reads — to be refined in Phase 8 with the rest of
the mental model; a first cut here):
```
Author or update an entity (a person, company, product, topic, etc.) you have
learned a fact about. Provide `ref` as `<type>:<slug>` (e.g. company:mxhero),
the display `name`, any `aliases`, `relations` to other entities
({to:"<type>:<slug>", type:"partner"}), and prose `body` describing what you
know. Merges into the existing entity if it exists; creates it otherwise. Do
NOT pass structured attributes — the system extracts those from your prose.
Use this for facts about THINGS; use memory_ingest for documents.
```

- [ ] **Test (tool authors an entity end-to-end)**
```python
import asyncio
from durin.agent.tools.memory_upsert_entity import MemoryUpsertEntityTool
from durin.memory.entity_page import EntityPage

def test_tool_authors_entity(tmp_path):
    tool = MemoryUpsertEntityTool(workspace=str(tmp_path))
    out = asyncio.run(tool.execute(
        ref="company:mxhero", name="mxHERO Inc.",
        aliases=["mxHERO"],
        relations=[{"to": "company:carahsoft", "type": "partner"}],
        body="Won the Box 2025 Solution Partner award."))
    assert out["ref"] == "company:mxhero"
    page = EntityPage.from_file(
        tmp_path / "memory/entities/company/mxhero.md")
    assert page.name == "mxHERO Inc."
    assert "mxHERO" in page.aliases
    assert any(r["to"] == "company:carahsoft" for r in page.relations)
    assert "Box 2025" in page.body
    # author recorded as field-level "agent" (via author_scope inside execute)
    assert page.provenance["relations"][0]["author"] == "agent"

def test_tool_merges_existing(tmp_path):
    tool = MemoryUpsertEntityTool(workspace=str(tmp_path))
    asyncio.run(tool.execute(ref="company:x", name="X", body="first"))
    asyncio.run(tool.execute(ref="company:x",
                             relations=[{"to": "topic:t", "type": "about"}]))
    page = EntityPage.from_file(tmp_path / "memory/entities/company/x.md")
    assert page.name == "X"                      # preserved
    assert any(r["to"] == "topic:t" for r in page.relations)
```

- [ ] **Impl** (mirror `memory_store` structure):
```python
@tool_parameters(_PARAMETERS)
class MemoryUpsertEntityTool(Tool):
    config_key = "memory"
    def __init__(self, workspace, app_config=None):
        self._workspace = Path(workspace).expanduser()
        self._app_config = app_config
    @property
    def name(self): return "memory_upsert_entity"
    @property
    def description(self): return _PARAMETERS["description"]
    @classmethod
    def create(cls, ctx):
        return cls(workspace=ctx.workspace, app_config=getattr(ctx, "app_config", None))
    async def execute(self, **kwargs):
        ref = (kwargs.get("ref") or "").strip()
        if not ref or ":" not in ref:
            return {"error": "ref must be '<type>:<slug>'"}
        patches = []
        now = datetime.now(timezone.utc)
        src = _session_turn_ref() or "memory_upsert_entity"   # reuse helper
        for a in (kwargs.get("aliases") or []):
            patches.append(FieldPatch(kind="alias", value=a, author=None, source_ref=src, at=now))
        for r in (kwargs.get("relations") or []):
            patches.append(FieldPatch(kind="relation", value=r, author=None, source_ref=src, at=now))
        body = kwargs.get("body")
        if body:
            patches.append(FieldPatch(kind="body_append", value=body, author=None, source_ref=src, at=now))
        with author_scope("agent_created"):
            result = write_entity(self._workspace, ref, patches,
                                  create=True, name=kwargs.get("name"))
        emit_tool_event("memory.upsert_entity",
                        {"ref": ref, "committed": result.committed, "retries": result.retries})
        return {"ref": ref, "committed": result.committed}
```
`_session_turn_ref` can be copied from `memory_store.py` (the per-turn
provenance helper) or imported if exported.

- [ ] **Run + commit**: `feat(tools): memory_upsert_entity — agent authors entities`

---

## Task 3: verify auto-registration

- [ ] **Test**: load the registry and assert `memory_upsert_entity` is present;
  assert `to_schema()` exposes the params (ref/name/aliases/relations/body) and
  the description. (Use the loader with a minimal ctx, or instantiate +
  `to_schema()` directly.)
- [ ] **Commit**: `test(tools): memory_upsert_entity registered + schema`

---

## Verification (end-to-end + live)

1. **Tool-execute path** (faithful to what the agent invokes): `execute(...)` →
   entity `.md` on disk + git commit + `author=agent` provenance + appears in the
   memory graph (`graph.py` reads files, no index needed).
2. **Live agent attempt:** run a real agent turn telling it to author an entity.
   **Honest expectation:** the agent's mental model (`identity.md`) still points
   at `memory_store`, not `memory_upsert_entity` — so natural routing needs
   Phase 8. The Phase-2 live test either (a) prompts the agent *explicitly* to
   use `memory_upsert_entity`, or (b) confirms the tool is callable and the
   execute path works. Report which, plainly.

## Open / deferred
- **Indexing** (FTS+vector) of the authored entity for *search* is deferred to
  the indexing/references phase; the graph view works without it.
- **Dangling relations** to non-existent targets are allowed (gap #1) — no
  placeholder creation here.
