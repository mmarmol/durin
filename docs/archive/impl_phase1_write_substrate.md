# Phase 1 — Write Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]` checkboxes. Run in a git worktree (`superpowers:using-git-worktrees`).

**Goal:** Build the optimistic, multi-writer entity-write substrate: per-field provenance with a 3-value author, precedence resolution (`user > dream > agent`), and a `memory_writer` that commits via dulwich plumbing + `set_if_equals` CAS (no working-tree race).

**Architecture:** All entity writes (agent, dream, dashboard) go through `memory_writer`. A write = read the page at `HEAD` → apply a *field-patch* with precedence against existing per-field provenance → build a commit via dulwich **plumbing** (object_store: Blob/Tree/Commit, no working-tree mutation) → `refs.set_if_equals(ref, old, new)` CAS → retry-with-reapply on conflict. The working tree is fast-forwarded to HEAD afterward (for Obsidian reading). Field-patch application logic is reused from the existing `dream_apply` jsonpatch path; the **commit mechanism** is new (today it is working-tree atomic-write + porcelain commit via the runner).

**Tech stack:** Python, `dulwich` 0.25.2 (`repo.object_store`, `dulwich.objects.{Blob,Tree,Commit}`, `repo.refs.set_if_equals`), `EntityPage` (`durin/memory/entity_page.py`), `jsonpatch`. Tests: pytest.

**Verified existing code this builds on:**
- `durin/memory/provenance.py` — page-level `Author = Literal["user_authored","agent_created"]` + `author_scope`/`current_author` ContextVar. **Unchanged**; the new field-level author is a *separate* 3-value notion (§2.4 of the design: the two coexist).
- `durin/memory/entity_page.py` — `EntityPage` with `provenance: dict[str,Any]` (schema-less), `author: str` (page-level), `from_text`/`to_markdown` round-trip; `extra` preserves emergent fields. **`provenance` is a free dict → the per-field author convention needs NO schema change.**
- `durin/memory/dream_apply.py:359-387` — current provenance shape is `{attributes:{key:{source_ref}}, relations:[{index, source_ref}]}` — **only `source_ref`, no `author`/`extracted_at`.** Phase 1 extends each entry to `{source_ref, extracted_at, author}`.
- `durin/utils/git_repo.py` / `gitstore.py` — use dulwich **porcelain** (working-tree add+commit). `memory_writer` uses **plumbing** instead (net-new).

**Design decision flagged for end-review:** `memory_writer` (plumbing+CAS) becomes the single entity-write path, *replacing* the working-tree-write+porcelain-commit that `dream_apply`+runner do today. `dream_apply`'s pure apply logic (`_apply_ops_to_page`) is reused as a building block; its `_atomic_write` + `.md.bak` working-tree path is superseded by the CAS commit. (Alternative considered: keep working-tree write for local, CAS only cross-host — rejected in §2.5 as a hybrid that doesn't unify.)

---

## File structure

| File | Responsibility |
|---|---|
| `durin/memory/field_provenance.py` *(new)* | `FieldAuthor` type; build/read a provenance entry `{source_ref, extracted_at, author}`; `resolve_precedence(...)`; helpers to read/write the per-field provenance map on a page. |
| `durin/memory/field_patch.py` *(new)* | `FieldPatch` dataclass (one structured edit: target path, value, author, source_ref, extracted_at) + `apply_field_patch(page, patch) -> bool` (applies with precedence, records provenance). Reuses `EntityPage`. |
| `durin/memory/git_plumbing.py` *(new)* | dulwich plumbing helpers: `read_blob_at_head(repo, rel_path) -> bytes|None`, `build_commit_with_file(repo, base_commit, rel_path, content, author, message) -> sha`, `head_sha(repo)`. No working-tree mutation. |
| `durin/memory/memory_writer.py` *(new)* | `write_entity(workspace, ref, patches, author) -> WriteResult`: read-base → apply patches (precedence) → plumbing commit → `set_if_equals` CAS → retry. Plus `fast_forward_working_tree(repo)`. |
| `tests/memory/test_field_provenance.py` *(new)* | precedence + provenance entry round-trip. |
| `tests/memory/test_field_patch.py` *(new)* | apply-with-precedence behavior. |
| `tests/memory/test_memory_writer.py` *(new)* | CAS write, concurrent-writer retry, idempotency. |

---

## Task 1: Field-level provenance type + entry helpers

**Files:** Create `durin/memory/field_provenance.py`; Test `tests/memory/test_field_provenance.py`

- [ ] **Step 1 — failing test**

```python
# tests/memory/test_field_provenance.py
from datetime import datetime, timezone
from durin.memory.field_provenance import make_entry, FieldAuthor

def test_make_entry_shape():
    e = make_entry(source_ref="[[sessions/s.md#turn-3]]",
                   author="agent",
                   at=datetime(2026, 6, 5, tzinfo=timezone.utc))
    assert e == {
        "source_ref": "[[sessions/s.md#turn-3]]",
        "extracted_at": "2026-06-05T00:00:00+00:00",
        "author": "agent",
    }

def test_make_entry_rejects_bad_author():
    import pytest
    with pytest.raises(ValueError):
        make_entry(source_ref="x", author="dream_bot", at=datetime.now(timezone.utc))
```

- [ ] **Step 2 — run, expect fail** — `pytest tests/memory/test_field_provenance.py -x` → ImportError / fail.

- [ ] **Step 3 — implement**

```python
# durin/memory/field_provenance.py
"""Per-field provenance: who set each attribute/relation, from where, when.

Distinct from the PAGE-level author (durin/memory/provenance.py, 2 values
user_authored/agent_created). This is FIELD-level, 3 values, and drives the
write-time precedence (user > dream > agent). The two coexist (design §2.4).
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Literal

FieldAuthor = Literal["user", "agent", "dream"]
_VALID_AUTHORS = frozenset({"user", "agent", "dream"})
# Precedence rank: higher wins. user > dream > agent.
_RANK: dict[str, int] = {"agent": 0, "dream": 1, "user": 2}

def make_entry(*, source_ref: str, author: str, at: datetime) -> dict[str, str]:
    if author not in _VALID_AUTHORS:
        raise ValueError(f"author {author!r} must be one of {sorted(_VALID_AUTHORS)}")
    return {
        "source_ref": source_ref,
        "extracted_at": at.isoformat(),
        "author": author,
    }

def author_rank(author: str) -> int:
    # Unknown/missing author ranks lowest so a well-formed write always wins.
    return _RANK.get(author, -1)
```

- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit**: `git add durin/memory/field_provenance.py tests/memory/test_field_provenance.py && git commit -m "feat(memory): field-level provenance entry helper (3-value author)"`

---

## Task 2: Precedence resolution

**Files:** Modify `durin/memory/field_provenance.py`; Test same file.

- [ ] **Step 1 — failing tests** (table of cases)

```python
# append to tests/memory/test_field_provenance.py
from datetime import timedelta
from durin.memory.field_provenance import incoming_wins, make_entry

NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=1)

def _e(author, at): return make_entry(source_ref="x", author=author, at=at)

def test_user_beats_dream_and_agent():
    assert incoming_wins(existing=_e("dream", NOW), incoming=_e("user", OLD)) is True
    assert incoming_wins(existing=_e("user", NOW), incoming=_e("dream", NOW)) is False

def test_dream_beats_agent():
    assert incoming_wins(existing=_e("agent", NOW), incoming=_e("dream", OLD)) is True

def test_same_level_recency_wins():
    assert incoming_wins(existing=_e("agent", OLD), incoming=_e("agent", NOW)) is True
    assert incoming_wins(existing=_e("agent", NOW), incoming=_e("agent", OLD)) is False

def test_no_existing_incoming_wins():
    assert incoming_wins(existing=None, incoming=_e("agent", NOW)) is True
```

- [ ] **Step 2 — run, expect fail.**

- [ ] **Step 3 — implement**

```python
# durin/memory/field_provenance.py  (append)
def incoming_wins(*, existing: dict[str, Any] | None,
                  incoming: dict[str, Any]) -> bool:
    """Decide whether `incoming` overwrites `existing` for one field.

    Rule (design §2.4): higher author-rank wins (user > dream > agent);
    same rank → newer `extracted_at` wins; missing existing → incoming wins.
    """
    if not existing:
        return True
    er = author_rank(str(existing.get("author", "")))
    ir = author_rank(str(incoming.get("author", "")))
    if ir != er:
        return ir > er
    # same rank: recency tiebreak (ISO strings sort chronologically)
    return str(incoming.get("extracted_at", "")) >= str(existing.get("extracted_at", ""))
```

- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(memory): per-field precedence resolution (user>dream>agent + recency)"`

> **Note on contradiction → temporal validity:** when `incoming_wins` is True AND the existing value differs semantically, the *old value* should move to history (`valid_from/valid_until`) for stateful attributes (design §4.3) rather than vanish. That belongs to the *dream refine* phase (Phase 4), not the raw write. Phase 1 records the winning value + provenance; it does not synthesize history. Leave a `# TODO(phase4): stateful history on contradiction` marker — **do not** implement it here.

---

## Task 3: `FieldPatch` + apply-with-precedence

**Files:** Create `durin/memory/field_patch.py`; Test `tests/memory/test_field_patch.py`

A `FieldPatch` is one structured edit the agent/dream emits. It targets an attribute key, a relation (add/dedup by `(to,type)`), an alias, or a body append. Apply respects precedence + records provenance.

- [ ] **Step 1 — failing test**

```python
# tests/memory/test_field_patch.py
from datetime import datetime, timezone
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch, apply_field_patch

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)

def _page():
    return EntityPage(type="company", name="mxHERO")

def test_agent_sets_attribute_records_provenance():
    p = _page()
    changed = apply_field_patch(p, FieldPatch(
        kind="attribute", key="hq", value="SF",
        author="agent", source_ref="[[sessions/s#turn-1]]", at=NOW))
    assert changed is True
    assert p.attributes["hq"] == "SF"
    assert p.provenance["attributes"]["hq"]["author"] == "agent"

def test_dream_overwrites_agent_same_field():
    p = _page()
    apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="SF",
        author="agent", source_ref="a", at=NOW))
    changed = apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="Boston",
        author="dream", source_ref="b", at=NOW))
    assert changed is True
    assert p.attributes["hq"] == "Boston"   # dream > agent
    assert p.provenance["attributes"]["hq"]["author"] == "dream"

def test_agent_cannot_overwrite_user_field():
    p = _page()
    apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="SF",
        author="user", source_ref="u", at=NOW))
    changed = apply_field_patch(p, FieldPatch(kind="attribute", key="hq", value="LA",
        author="agent", source_ref="a", at=NOW))
    assert changed is False              # user > agent → no overwrite
    assert p.attributes["hq"] == "SF"

def test_relation_add_dedup_by_to_type():
    p = _page()
    rel = dict(to="company:carahsoft", type="partner")
    apply_field_patch(p, FieldPatch(kind="relation", value=rel,
        author="agent", source_ref="a", at=NOW))
    apply_field_patch(p, FieldPatch(kind="relation", value=dict(rel),
        author="agent", source_ref="a2", at=NOW))
    assert len([r for r in p.relations if r["to"] == "company:carahsoft"]) == 1
```

- [ ] **Step 2 — run, expect fail.**

- [ ] **Step 3 — implement** (real code; relation dedup + body append are straightforward)

```python
# durin/memory/field_patch.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from durin.memory.entity_page import EntityPage
from durin.memory.field_provenance import make_entry, incoming_wins

PatchKind = Literal["attribute", "relation", "alias", "body_append"]

@dataclass
class FieldPatch:
    kind: PatchKind
    author: str
    source_ref: str
    at: datetime
    key: str | None = None          # attribute key
    value: Any = None               # attribute value / relation dict / alias str / body text

def apply_field_patch(page: EntityPage, patch: FieldPatch) -> bool:
    """Apply one patch to `page` in place, respecting precedence. Returns
    True if the page changed. Records per-field provenance for attribute/relation."""
    entry = make_entry(source_ref=patch.source_ref, author=patch.author, at=patch.at)
    prov = page.provenance or {}

    if patch.kind == "attribute":
        existing = (prov.get("attributes") or {}).get(patch.key)
        if not incoming_wins(existing=existing, incoming=entry):
            return False
        page.attributes[patch.key] = patch.value
        prov.setdefault("attributes", {})[patch.key] = entry
        page.provenance = prov
        return True

    if patch.kind == "relation":
        to, rtype = patch.value.get("to"), patch.value.get("type")
        for r in page.relations:                      # dedup by (to, type)
            if r.get("to") == to and r.get("type") == rtype:
                return False
        page.relations.append(dict(patch.value))
        prov.setdefault("relations", []).append({"index": len(page.relations) - 1, **entry})
        page.provenance = prov
        return True

    if patch.kind == "alias":
        if patch.value in page.aliases:
            return False
        page.aliases.append(patch.value)
        return True

    if patch.kind == "body_append":
        sep = "\n\n" if page.body and not page.body.endswith("\n") else "\n"
        marker = f"<!-- {patch.author} {patch.source_ref} -->"
        page.body = (page.body + sep + marker + "\n" + str(patch.value)).rstrip("\n")
        return True

    raise ValueError(f"unknown patch kind {patch.kind!r}")
```

- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(memory): FieldPatch apply-with-precedence + provenance recording"`

---

## Task 4: dulwich plumbing helpers (no working-tree mutation)

**Files:** Create `durin/memory/git_plumbing.py`; Test `tests/memory/test_memory_writer.py` (shared)

These read/write git **objects** directly, never touching the working tree. The memory git repo is `workspace/memory/.git`.

- [ ] **Step 1 — failing test**

```python
# tests/memory/test_memory_writer.py
from pathlib import Path
from dulwich import porcelain
from durin.memory.git_plumbing import head_sha, read_blob_at_head, build_commit_with_file

def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    root.mkdir()
    porcelain.init(str(root))
    (root / "seed.md").write_text("seed", encoding="utf-8")
    porcelain.add(str(root), paths=[str(root / "seed.md")])
    porcelain.commit(str(root), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    return root

def test_plumbing_roundtrip(tmp_path):
    root = _init_repo(tmp_path)
    base = head_sha(root)
    assert read_blob_at_head(root, "seed.md") == b"seed"
    assert read_blob_at_head(root, "missing.md") is None
    new = build_commit_with_file(root, base, "entities/company/x.md",
                                 b"hello", author=b"agent <a@a>", message=b"add x")
    # build_commit_with_file does NOT move the ref or working tree:
    assert head_sha(root) == base
    # but the new commit's tree contains the file:
    assert read_blob_at_head(root, "seed.md") == b"seed"  # head unchanged
```

- [ ] **Step 2 — run, expect fail.**

- [ ] **Step 3 — implement**

```python
# durin/memory/git_plumbing.py
from __future__ import annotations
import time
from pathlib import Path
from dulwich.repo import Repo
from dulwich.objects import Blob, Tree, Commit

_REF = b"refs/heads/main"   # NOTE: confirm the repo's default branch name at init time

def _open(root: Path) -> Repo:
    return Repo(str(root))

def head_sha(root: Path) -> bytes | None:
    repo = _open(root)
    try:
        return repo.refs.read_ref(_default_ref(repo))
    finally:
        repo.close()

def _default_ref(repo: Repo) -> bytes:
    # dulwich may init `master` or `main`; resolve HEAD's symref once.
    head = repo.refs.follow(b"HEAD")[0]          # returns (refnames, sha)
    return head[-1] if head else _REF

def read_blob_at_head(root: Path, rel_path: str) -> bytes | None:
    repo = _open(root)
    try:
        sha = repo.refs.read_ref(_default_ref(repo))
        if sha is None:
            return None
        return _read_blob(repo, sha, rel_path)
    finally:
        repo.close()

def _read_blob(repo: Repo, commit_sha: bytes, rel_path: str) -> bytes | None:
    commit = repo.object_store[commit_sha]
    tree = repo.object_store[commit.tree]
    parts = rel_path.encode().split(b"/")
    cur = tree
    for i, part in enumerate(parts):
        if part not in [e.path for e in cur.items()]:
            return None
        mode, sha = cur[part]
        obj = repo.object_store[sha]
        if i == len(parts) - 1:
            return obj.data if isinstance(obj, Blob) else None
        cur = obj  # descend into subtree
    return None

def build_commit_with_file(root: Path, base_commit: bytes | None,
                           rel_path: str, content: bytes,
                           *, author: bytes, message: bytes) -> bytes:
    """Build (in object store) a commit = base_commit with rel_path set to content.
    Returns the new commit sha. Does NOT move any ref or touch the working tree.
    Builds new tree objects along the path (copy-on-write up the directory chain)."""
    repo = _open(root)
    try:
        store = repo.object_store
        blob = Blob.from_string(content)
        store.add_object(blob)
        parts = rel_path.encode().split(b"/")
        base_tree = store[store[base_commit].tree] if base_commit else Tree()
        new_root = _set_path(store, base_tree, parts, blob.id)
        store.add_object(new_root)
        commit = Commit()
        commit.tree = new_root.id
        commit.parents = [base_commit] if base_commit else []
        commit.author = commit.committer = author
        commit.author_time = commit.commit_time = int(time.time())
        commit.author_timezone = commit.commit_timezone = 0
        commit.encoding = b"utf-8"
        commit.message = message
        store.add_object(commit)
        return commit.id
    finally:
        repo.close()

def _set_path(store, tree: Tree, parts: list[bytes], blob_id: bytes) -> Tree:
    """Return a NEW Tree with parts->blob_id set (recursively rebuilding subtrees)."""
    new = Tree()
    for entry in tree.items():
        new.add(entry.path, entry.mode, entry.sha)
    if len(parts) == 1:
        new.add(parts[0], 0o100644, blob_id)        # regular file mode
    else:
        head, rest = parts[0], parts[1:]
        sub = (store[tree[head][1]] if head in [e.path for e in tree.items()]
               else Tree())
        new_sub = _set_path(store, sub, rest, blob_id)
        store.add_object(new_sub)
        new.add(head, 0o040000, new_sub.id)          # directory mode
    return new
```

> **Verify at impl time:** the exact dulwich 0.25.2 API for `repo.refs.follow` / `read_ref` and `Tree.items()`/`Tree.add` — adjust the helper calls to the installed signatures (confirmed available: `Tree.add(name, mode, hexsha)`, `refs.set_if_equals(name, old, new)`). `Tree.items()` returns `TreeEntry(path, mode, sha)`. If `follow` differs, resolve the branch via `repo.head()` / `repo.refs[b"HEAD"]`.

- [ ] **Step 4 — run, expect pass** (fix API calls against installed dulwich until green).
- [ ] **Step 5 — commit**: `git commit -am "feat(memory): dulwich plumbing helpers (read blob, build commit, no worktree)"`

---

## Task 5: CAS write loop + working-tree fast-forward

**Files:** Create `durin/memory/memory_writer.py`; Test `tests/memory/test_memory_writer.py`

- [ ] **Step 1 — failing test (concurrent-writer retry)**

```python
# append to tests/memory/test_memory_writer.py
from datetime import datetime, timezone
from durin.memory.memory_writer import write_entity
from durin.memory.field_patch import FieldPatch
from durin.memory.entity_page import EntityPage

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)

def test_two_writers_different_fields_both_land(tmp_path):
    ws = tmp_path
    (ws / "memory").mkdir()
    # init repo + the entity must exist (create via first write)
    write_entity(ws, "company:mxhero",
                 [FieldPatch(kind="body_append", value="seed", author="agent",
                             source_ref="s", at=NOW)], create=True)
    # simulate concurrency: writer A relation, writer B alias, sequential calls
    write_entity(ws, "company:mxhero",
                 [FieldPatch(kind="relation", value=dict(to="company:carahsoft", type="partner"),
                             author="agent", source_ref="a", at=NOW)])
    write_entity(ws, "company:mxhero",
                 [FieldPatch(kind="alias", value="mxHERO Inc.", author="agent",
                             source_ref="b", at=NOW)])
    page = EntityPage.from_file(ws / "memory" / "entities" / "company" / "mxhero.md")
    assert any(r["to"] == "company:carahsoft" for r in page.relations)
    assert "mxHERO Inc." in page.aliases
```

- [ ] **Step 2 — run, expect fail.**

- [ ] **Step 3 — implement** (the CAS loop is the load-bearing code; written in full)

```python
# durin/memory/memory_writer.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from dulwich.repo import Repo
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch, apply_field_patch
from durin.memory.git_plumbing import head_sha, read_blob_at_head, build_commit_with_file, _default_ref

_MAX_RETRIES = 8

@dataclass
class WriteResult:
    ref: str
    committed: bool
    retries: int

def _rel_path(ref: str) -> str:
    type_, _, slug = ref.partition(":")
    return f"entities/{type_}/{slug}.md"

def write_entity(workspace: Path, ref: str, patches: list[FieldPatch],
                 *, create: bool = False) -> WriteResult:
    """Optimistic write: read page@HEAD -> apply patches (precedence) ->
    plumbing commit -> set_if_equals CAS -> retry on conflict. Then ff worktree."""
    root = Path(workspace) / "memory"
    rel = _rel_path(ref)
    type_, _, slug = ref.partition(":")
    for attempt in range(_MAX_RETRIES):
        base = head_sha(root)
        raw = read_blob_at_head(root, rel)
        if raw is None:
            if not create and attempt == 0:
                raise FileNotFoundError(f"entity {ref} not found (pass create=True to author it)")
            page = EntityPage(type=type_, name=slug, created_at=datetime.now(timezone.utc))
        else:
            page = EntityPage.from_text(raw.decode("utf-8")) or EntityPage(type=type_, name=slug)
        changed = False
        for p in patches:
            changed |= apply_field_patch(page, p)
        page.updated_at = datetime.now(timezone.utc)
        if not changed and raw is not None:
            return WriteResult(ref, committed=False, retries=attempt)  # no-op
        content = page.to_markdown().encode("utf-8")
        new_commit = build_commit_with_file(
            root, base, rel, content,
            author=b"durin-memory <memory@durin.local>",
            message=f"upsert {ref}".encode())
        repo = Repo(str(root))
        try:
            ok = repo.refs.set_if_equals(_default_ref(repo), base, new_commit)
        finally:
            repo.close()
        if ok:
            fast_forward_working_tree(root)
            return WriteResult(ref, committed=True, retries=attempt)
        # CAS failed: HEAD moved under us → loop re-reads + re-applies.
    raise RuntimeError(f"write_entity({ref}) exceeded {_MAX_RETRIES} CAS retries")

def fast_forward_working_tree(root: Path) -> None:
    """Reset the working tree to HEAD so Obsidian/readers see the latest.
    Best-effort; skip files with uncommitted human edits (design §2.5, 2D-1)."""
    from dulwich import porcelain
    # MVP: dulwich.porcelain.reset --hard equivalent; refine in Phase (human-edit).
    porcelain.reset(str(root), mode="hard")
```

> **Verify at impl time:** `porcelain.reset(..., mode="hard")` exists in dulwich 0.25.2 (it does). The "skip dirty human edits" refinement (2D-1) is explicitly **deferred** to the references/human-edit work — here a hard reset is acceptable because all writes go through `memory_writer` (no concurrent human edit in the test/MVP path). Leave `# TODO(human-edit): don't ff over dirty working tree`.

- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(memory): memory_writer optimistic CAS write loop + worktree ff"`

---

## Task 6: Concurrency + idempotency tests (harden the CAS)

**Files:** Test only (`tests/memory/test_memory_writer.py`).

- [ ] **Step 1 — tests**

```python
def test_idempotent_attribute_reapply(tmp_path):
    ws = tmp_path; (ws / "memory").mkdir()
    write_entity(ws, "company:x",
                 [FieldPatch(kind="attribute", key="hq", value="SF",
                             author="dream", source_ref="s", at=NOW)], create=True)
    # re-apply identical dream patch → no duplicate, value stable (3A-1 idempotency)
    r = write_entity(ws, "company:x",
                     [FieldPatch(kind="attribute", key="hq", value="SF",
                                 author="dream", source_ref="s", at=NOW)])
    page = EntityPage.from_file(ws / "memory/entities/company/x.md")
    assert page.attributes["hq"] == "SF"
    # set-of-same-value is still a "win" by recency-equal; assert no crash + single key
    assert list(page.attributes.keys()) == ["hq"]

def test_real_concurrency_threads(tmp_path):
    import threading
    ws = tmp_path; (ws / "memory").mkdir()
    write_entity(ws, "company:x",
                 [FieldPatch(kind="body_append", value="seed", author="agent",
                             source_ref="s", at=NOW)], create=True)
    def worker(i):
        write_entity(ws, "company:x",
                     [FieldPatch(kind="relation",
                                 value=dict(to=f"topic:t{i}", type="rel"),
                                 author="agent", source_ref=f"s{i}", at=NOW)])
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    page = EntityPage.from_file(ws / "memory/entities/company/x.md")
    assert len({r["to"] for r in page.relations}) == 8   # all 8 landed via CAS retry
```

- [ ] **Step 2 — run.** If the threaded test flakes, the CAS loop or `set_if_equals` usage is wrong — debug (`superpowers:systematic-debugging`) until 8/8 land deterministically.
- [ ] **Step 3 — commit**: `git commit -am "test(memory): CAS concurrency (8 threads) + idempotent re-apply"`

---

## Task 7: Wire `author_scope` → FieldPatch author (bridge to existing ContextVar)

The existing per-write paths declare a *page-level* author via `author_scope("agent_created"|"user_authored")`. Map it to the field-level 3-value author for patches that don't carry one explicitly.

**Files:** Modify `durin/memory/memory_writer.py`; Test same.

- [ ] **Step 1 — test**

```python
def test_default_field_author_from_scope(tmp_path):
    from durin.memory.provenance import author_scope
    ws = tmp_path; (ws / "memory").mkdir()
    with author_scope("agent_created"):
        write_entity(ws, "company:x",
                     [FieldPatch(kind="attribute", key="hq", value="SF",
                                 author=None, source_ref="s", at=NOW)], create=True)  # author None -> resolve
    page = EntityPage.from_file(ws / "memory/entities/company/x.md")
    assert page.provenance["attributes"]["hq"]["author"] == "agent"
```

- [ ] **Step 2 — implement**: in `write_entity`, before applying, resolve any `patch.author is None` to the field-level author derived from `current_author()` (`agent_created → agent`, `user_authored → user`); `dream` is passed explicitly by the dream paths. Make `FieldPatch.author` `str | None` and add a `_resolve_author()` helper:

```python
# field_patch.py: allow None; memory_writer resolves it.
# memory_writer.py:
from durin.memory.provenance import current_author, MissingAuthorScopeError
_PAGE_TO_FIELD = {"agent_created": "agent", "user_authored": "user"}
def _resolve_author(a):
    if a is not None:
        return a
    try:
        return _PAGE_TO_FIELD[current_author()]
    except MissingAuthorScopeError:
        raise  # writes must declare authorship (matches provenance.py contract)
```
Apply `_resolve_author` to each patch's `author` at the top of the retry loop.

- [ ] **Step 3 — run, expect pass.**
- [ ] **Step 4 — commit**: `git commit -am "feat(memory): bridge page-level author_scope to field-level patch author"`

---

## Self-review checklist (run after writing the code)

- [ ] **Spec coverage:** §2.4 per-field provenance ✓ (T1-3,7), precedence user>dream>agent ✓ (T2), §2.5 optimistic CAS via `set_if_equals` + plumbing ✓ (T4-5), retry/re-apply ✓ (T5-6), idempotency (3A-1) ✓ (T6). **Not in Phase 1 (correct):** working-tree ff over dirty human edits (deferred), contradiction→temporal-validity (Phase 4), the `memory_upsert_entity` tool (Phase 2), removing `dream_apply`'s old commit path (Phase 8 cleanup — Phase 1 leaves both coexisting; nothing calls `memory_writer` in prod yet).
- [ ] **Default branch name:** confirm whether `memory/.git` inits `main` or `master` (dulwich default) — `_default_ref` resolves HEAD's symref so it works either way; add a test asserting it.
- [ ] **No placeholders:** the only `# TODO(...)` markers are intentional cross-phase deferrals, each naming the phase. No vague "add error handling."
- [ ] **Type consistency:** `FieldAuthor` ("user"/"agent"/"dream") vs page-level `Author` ("user_authored"/"agent_created") are distinct on purpose; `_PAGE_TO_FIELD` is the only bridge.

## Open decision for end-review

`write_entity` does a **hard `porcelain.reset`** to ff the working tree (Task 5). That is safe *only because in Phase 1 nothing else writes the working tree*. The proper "don't ff over dirty" logic (design 2D-1) lands with the human-edit/references work. Flag: is a hard-reset ff acceptable as the Phase-1 stand-in, or do we want the dirty-check guard from day one? (Recommendation: stand-in now, guard in the references phase.)
