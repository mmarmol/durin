# Skills Evolution MVP — Implementation Plan

> ✅ **EJECUTADO — E1 SHIPPED** (PR #19, merge `e595fd6`). Plan histórico, cerrado.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make durin's skills first-class **versioned, mode-gated entities** — the agent evolves them in-loop via a `skill_edit` tool that commits each change (with rationale, reversible), an `auto|manual` flag per skill controls who may edit, and a `/skills` command + web panel let the user see/edit/toggle them.

**Architecture:** All domain logic lives in one service module `durin/agent/skills_store.py` (operations) backed by the existing `GitStore` extended with a **`subtree` flag** (whole-tree git for `workspace/skills/`, separate repo from the memory git). The store is **eager-initialized at workspace setup**. Three thin adapters call the service: the `skill_edit` tool (auto-discovered), the `/skills` slash-command, and GET-only web routes in the websockets channel. The webui gets a `SkillsSettings` panel. Spec: [`docs/superpowers/specs/2026-06-01-skills-evolution-mvp-design.md`](../specs/2026-06-01-skills-evolution-mvp-design.md).

**Tech Stack:** Python 3 · dulwich (git) · PyYAML (frontmatter) · pytest (`asyncio_mode=auto`) · aiohttp-free websockets channel · React + Vite + bun (webui).

---

## Deltas discovered during planning (vs spec)

Factual refinements from reading the real code; they sharpen the spec, scope unchanged:

1. **Tools are auto-discovered** ([`durin/agent/tools/loader.py`](../../../durin/agent/tools/loader.py)) — creating `durin/agent/tools/skill_edit.py` IS the registration. No registry list to edit.
2. **Web API is GET-only.** The settings REST API lives in the **websockets** channel ([`durin/channels/websocket.py`](../../../durin/channels/websocket.py)), whose HTTP parser only accepts GET. Mutations are `GET + query params` (mirroring `/api/config/set`, `/api/cron/toggle`): `GET /api/skills/{name}/save?content=...` and `GET /api/skills/{name}/mode?value=...`. SKILL.md content in a query string is fine (skills are small KB); noted caveat.
3. **Versioning = a `subtree` flag on the existing `GitStore`, not a new class** (user decision — avoid duplicating `log`/`_resolve_sha`/`diff_commits`). `subtree=False` default keeps the memory store byte-identical; the existing `tests/utils/test_gitstore.py` is the regression guard.
4. **Eager init** at `sync_workspace_templates()` ([`durin/utils/helpers.py:742`](../../../durin/utils/helpers.py#L742)), alongside the memory store's `gs.init()`. Lazy init stays as a safety net inside `auto_commit`.
5. **Mode/provenance persistence = frontmatter** (`metadata.durin.mode` / `metadata.durin.provenance`). No sidecar.

---

## File structure

| File | Responsibility | Status |
|---|---|---|
| `durin/utils/gitstore.py` | Add a `subtree` flag (whole-tree mode) to `GitStore` | modify |
| `durin/utils/helpers.py` | Eager-init the skills store in `sync_workspace_templates()` | modify |
| `durin/agent/skills_frontmatter.py` | Split/join YAML frontmatter; durin-namespace helpers | **new** |
| `durin/agent/skills_store.py` | Service: list/read/fork-on-write/read_mode/set_mode/apply_edit/save + web glue | **new** |
| `durin/agent/tools/skill_edit.py` | `skill_edit` tool (thin adapter → service) | **new** |
| `durin/command/builtin.py` | `/skills` command handler + registration + palette spec | modify |
| `durin/channels/websocket.py` | 4 GET routes + handlers (thin → service) | modify |
| `webui/src/lib/api.ts` | `listSkills/getSkill/saveSkill/setSkillMode` + types | modify |
| `webui/src/components/settings/SkillsSettings.tsx` | Panel: list + view + edit manual + toggle mode | **new** |
| `webui/src/components/settings/SettingsView.tsx` | Register the `skills` tab | modify |
| `webui/src/i18n/locales/*` | `settings.nav.skills` key | modify |

Tests mirror the package layout under `tests/`. Run: `pytest tests/ -q --maxfail=5`.

---

# MILESTONE A — Versioning + service core (backend, fully unit-tested)

## Task 1: Extend `GitStore` with a `subtree` flag (whole-tree mode)

**Files:**
- Modify: `durin/utils/gitstore.py`
- Test: `tests/utils/test_gitstore.py` (add `TestSubtreeMode`; keep existing tests as the regression guard)

- [ ] **Step 1: Write the failing test** (append to `tests/utils/test_gitstore.py`)

```python
# tests/utils/test_gitstore.py  (append)
from pathlib import Path

from durin.utils.gitstore import GitStore


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestSubtreeMode:
    def _store(self, root: Path) -> GitStore:
        s = GitStore(root, subtree=True, label="skills")
        s.init()
        return s

    def test_init_creates_repo_and_permissive_gitignore(self, tmp_path):
        root = tmp_path / "skills"
        s = GitStore(root, subtree=True, label="skills")
        assert s.init() is True
        assert (root / ".git").is_dir()
        assert "__pycache__/" in (root / ".gitignore").read_text(encoding="utf-8")
        assert s.init() is False  # idempotent

    def test_commit_tracks_arbitrary_files(self, tmp_path):
        root = tmp_path / "skills"
        s = self._store(root)
        _write(root, "a/SKILL.md", "# a\n")
        sha = s.auto_commit("skill(a): create")
        assert sha and len(sha) == 8
        assert s.auto_commit("noop") is None
        assert s.log()[0].message == "skill(a): create"

    def test_revert_undoes_modification_and_addition(self, tmp_path):
        root = tmp_path / "skills"
        s = self._store(root)
        _write(root, "a/SKILL.md", "original\n")
        s.auto_commit("skill(a): create")
        _write(root, "a/SKILL.md", "changed\n")
        _write(root, "b/SKILL.md", "b\n")
        bad = s.auto_commit("skill: bad")
        s.revert(bad)
        assert (root / "a" / "SKILL.md").read_text(encoding="utf-8") == "original\n"
        assert not (root / "b" / "SKILL.md").exists()  # addition undone
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/utils/test_gitstore.py::TestSubtreeMode -q`
Expected: FAIL — `GitStore.__init__() got an unexpected keyword argument 'subtree'`.

- [ ] **Step 3: Apply these edits to `durin/utils/gitstore.py`**

(3a) Replace `__init__` (currently `def __init__(self, workspace, tracked_files)`):

```python
    def __init__(
        self,
        workspace: Path,
        tracked_files: list[str] | None = None,
        *,
        subtree: bool = False,
        label: str = "memory",
    ):
        self._workspace = workspace
        self._tracked_files = tracked_files or []
        self._subtree = subtree
        self._label = label
        # Memory keeps its historical author; subtree stores get a per-label one.
        self._author = (
            f"durin <durin@{label}>".encode() if subtree else b"durin <durin@dream>"
        )
```

(3b) Add a staging helper + the subtree tree walkers (anywhere among the internal helpers):

```python
    def _stage_all(self) -> list[str]:
        """Stage every change (new, modified, deleted). Returns changed relpaths."""
        from dulwich import porcelain
        from dulwich.repo import Repo

        changed: set[str] = set()
        with Repo(str(self._workspace)) as repo:
            st = porcelain.status(repo)
            for key in ("add", "modify", "delete"):
                for p in st.staged.get(key, []):
                    changed.add(p.decode() if isinstance(p, bytes) else p)
            for p in list(st.unstaged) + list(st.untracked):
                changed.add(p.decode() if isinstance(p, bytes) else p)
            if changed:
                repo.stage(sorted(changed))
        return sorted(changed)

    @staticmethod
    def _iter_tree(repo, tree, prefix: str = ""):
        for entry in tree.items():
            name = entry.path.decode() if isinstance(entry.path, bytes) else entry.path
            rel = f"{prefix}{name}"
            obj = repo[entry.sha]
            if obj.type_name == b"tree":
                yield from GitStore._iter_tree(repo, obj, prefix=rel + "/")
            elif obj.type_name == b"blob":
                yield rel, obj.data

    def _sync_worktree(self, target: dict[str, bytes]) -> None:
        for rel, data in target.items():
            dest = self._workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        for path in self._workspace.rglob("*"):
            if path.is_dir():
                continue
            if ".git" in path.relative_to(self._workspace).parts:
                continue
            rel = str(path.relative_to(self._workspace))
            if rel not in target:
                path.unlink()
```

(3c) In `_build_gitignore`, add a subtree branch at the very top:

```python
    def _build_gitignore(self) -> str:
        if self._subtree:
            return "__pycache__/\n*.pyc\n.archive/\n.DS_Store\n"
        # ... existing whitelist logic unchanged ...
```

(3d) In `init`, replace the "ensure tracked files exist + add" block + the commit message. The new staging branch:

```python
            # (after writing .gitignore)
            if self._subtree:
                self._stage_all()
            else:
                for rel in self._tracked_files:
                    p = self._workspace / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not p.exists():
                        p.write_text("", encoding="utf-8")
                porcelain.add(str(self._workspace), paths=[".gitignore"] + self._tracked_files)

            porcelain.commit(
                str(self._workspace),
                message=f"init: durin {self._label} store".encode("utf-8"),
                author=self._author,
                committer=self._author,
            )
```

> `label="memory"` default ⇒ the message stays `init: durin memory store`, byte-identical to today.

(3e) In `auto_commit`, replace the staging section and use `self._author`:

```python
            if self._subtree:
                if not self._stage_all():
                    return None
            else:
                st = porcelain.status(str(self._workspace))
                if not st.unstaged and not any(st.staged.values()):
                    return None
                porcelain.add(str(self._workspace), paths=self._tracked_files)

            msg_bytes = message.encode("utf-8") if isinstance(message, str) else message
            sha_bytes = porcelain.commit(
                str(self._workspace),
                message=msg_bytes,
                author=self._author,
                committer=self._author,
            )
```

(3f) In `revert`, branch before the enumerated-files restore. After resolving `commit_obj`, its parent, and `tree = repo[parent_obj.tree]`:

```python
                if self._subtree:
                    target = dict(self._iter_tree(repo, tree))
                    # (close the Repo context, then sync + commit outside)
```
Concretely, restructure the subtree path to:
```python
            with Repo(str(self._workspace)) as repo:
                commit_obj = repo[full_sha]
                if commit_obj.type_name != b"commit" or not commit_obj.parents:
                    return None
                parent_obj = repo[commit_obj.parents[0]]
                tree = repo[parent_obj.tree]
                if self._subtree:
                    target = dict(self._iter_tree(repo, tree))
                else:
                    target = None
                    restored: list[str] = []
                    for filepath in self._tracked_files:
                        content = self._read_blob_from_tree(repo, tree, filepath)
                        if content is not None:
                            dest = self._workspace / filepath
                            dest.write_text(content, encoding="utf-8")
                            restored.append(filepath)

            if self._subtree:
                self._sync_worktree(target)
                return self.auto_commit(f"revert: undo {commit}")
            if not restored:
                return None
            return self.auto_commit(f"revert: undo {commit}")
```

> The existing `revert` already commits via `self.auto_commit(...)`; keep that. The only additions are the `self._subtree` branches.

- [ ] **Step 4: Run tests to verify they pass (new + regression)**

Run: `pytest tests/utils/test_gitstore.py -q`
Expected: PASS — `TestSubtreeMode` (3 tests) green AND all pre-existing GitStore tests still green.

- [ ] **Step 5: Commit**

```bash
git add durin/utils/gitstore.py tests/utils/test_gitstore.py
git commit -m "feat(gitstore): subtree mode (whole-tree versioning) behind a flag"
```

---

## Task 1b: Eager-init the skills store at workspace setup

**Files:**
- Modify: `durin/utils/helpers.py` — in `sync_workspace_templates()` (~line 775, right after the memory `gs.init()`)
- Test: `tests/utils/test_skills_eager_init.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/utils/test_skills_eager_init.py
from durin.utils.helpers import sync_workspace_templates


def test_workspace_setup_inits_skills_store(tmp_path):
    sync_workspace_templates(tmp_path, silent=True)
    assert (tmp_path / "skills").is_dir()
    assert (tmp_path / "skills" / ".git").is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/utils/test_skills_eager_init.py -q`
Expected: FAIL — `tmp_path/skills/.git` does not exist.

- [ ] **Step 3: Add the eager-init** right after the memory store's `gs.init()` try/except block:

```python
    # Initialize git for skill version control (separate repo, whole-tree)
    try:
        from durin.utils.gitstore import GitStore

        GitStore(workspace / "skills", subtree=True, label="skills").init()
    except Exception:
        logger.exception("Failed to initialize skills git store for {}", workspace)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/utils/test_skills_eager_init.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add durin/utils/helpers.py tests/utils/test_skills_eager_init.py
git commit -m "feat(skills): eager-init the skills git store at workspace setup"
```

---

## Task 2: Frontmatter helpers

**Files:**
- Create: `durin/agent/skills_frontmatter.py`
- Test: `tests/agent/test_skills_frontmatter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skills_frontmatter.py
from durin.agent.skills_frontmatter import (
    ensure_durin,
    join_frontmatter,
    split_frontmatter,
)


def test_split_returns_data_and_body():
    text = "---\nname: x\ndescription: d\n---\nBODY\n"
    data, body = split_frontmatter(text)
    assert data["name"] == "x"
    assert body == "BODY\n"


def test_split_no_frontmatter_returns_empty_dict():
    data, body = split_frontmatter("no frontmatter here")
    assert data == {}
    assert body == "no frontmatter here"


def test_round_trip_preserves_body_and_adds_durin_field():
    text = "---\nname: x\ndescription: d\n---\nBODY\n"
    data, body = split_frontmatter(text)
    ensure_durin(data)["mode"] = "auto"
    out = join_frontmatter(data, body)
    data2, body2 = split_frontmatter(out)
    assert body2 == "BODY\n"
    assert data2["metadata"]["durin"]["mode"] == "auto"
    assert data2["name"] == "x"


def test_ensure_durin_coerces_non_dict_metadata():
    data = {"metadata": "garbage"}
    durin = ensure_durin(data)
    durin["mode"] = "manual"
    assert data["metadata"]["durin"]["mode"] == "manual"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skills_frontmatter.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# durin/agent/skills_frontmatter.py
"""Read/write helpers for SKILL.md YAML frontmatter (durin namespace)."""
from __future__ import annotations

import re

import yaml

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_FM_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Empty dict if no frontmatter."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):
        data = {}
    return data, text[m.end():]


def join_frontmatter(data: dict, body: str) -> str:
    """Rebuild a SKILL.md string from frontmatter dict + body."""
    fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body}"


def ensure_durin(data: dict) -> dict:
    """Return data['metadata']['durin'], creating/repairing the path in place."""
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
        data["metadata"] = meta
    durin = meta.get("durin")
    if not isinstance(durin, dict):
        durin = {}
        meta["durin"] = durin
    return durin
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skills_frontmatter.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_frontmatter.py tests/agent/test_skills_frontmatter.py
git commit -m "feat(skills): SKILL.md frontmatter read/write helpers"
```

---

## Task 3: skills_store service — list / read / fork-on-write / read_mode

**Files:**
- Create: `durin/agent/skills_store.py`
- Test: `tests/agent/test_skills_store_read.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skills_store_read.py
from pathlib import Path

from durin.agent import skills_store as ss


def _make_builtin(tmp_path: Path) -> Path:
    b = tmp_path / "builtin"
    (b / "greet").mkdir(parents=True)
    (b / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hi\n---\nBody\n", encoding="utf-8"
    )
    return b


def _make_user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: mine\n---\nBody\n", encoding="utf-8"
    )


def test_read_mode_defaults_builtin_auto_user_manual(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", _make_builtin(tmp_path))
    _make_user_skill(ws, "mine")
    assert ss.read_mode(ws, "greet") == "auto"
    assert ss.read_mode(ws, "mine") == "manual"


def test_fork_on_write_copies_builtin_and_stamps_provenance(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    builtin = _make_builtin(tmp_path)
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", builtin)
    dest = ss.fork_on_write(ws, "greet")
    assert (dest / "SKILL.md").exists()
    assert (builtin / "greet" / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert ss.read_mode(ws, "greet") == "auto"
    info = {s["name"]: s for s in ss.list_skills_info(ws)}
    assert info["greet"]["provenance"]["source"] == "builtin:greet"


def test_list_skills_info_reports_source_and_mode(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(ss, "BUILTIN_SKILLS_DIR", _make_builtin(tmp_path))
    _make_user_skill(ws, "mine")
    by_name = {s["name"]: s for s in ss.list_skills_info(ws)}
    assert by_name["greet"]["source"] == "builtin"
    assert by_name["greet"]["mode"] == "auto"
    assert by_name["mine"]["source"] == "workspace"
    assert by_name["mine"]["mode"] == "manual"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skills_store_read.py -q`
Expected: FAIL — `skills_store` / functions missing.

- [ ] **Step 3: Write the implementation (read side)**

```python
# durin/agent/skills_store.py
"""Service layer for durin's skill versioning + mode system.

All skill mutations go through here so the tool, the /skills command, and the
web routes share one implementation (and one git store). Pure functions over a
workspace Path — directly unit-testable with tmp_path.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import shutil
from pathlib import Path

from durin.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader
from durin.agent.skills_frontmatter import ensure_durin, join_frontmatter, split_frontmatter
from durin.utils.gitstore import GitStore


def _skills_dir(workspace: Path) -> Path:
    return Path(workspace) / "skills"


def _skill_md(workspace: Path, name: str) -> Path:
    return _skills_dir(workspace) / name / "SKILL.md"


def _store(workspace: Path) -> GitStore:
    return GitStore(_skills_dir(workspace), subtree=True, label="skills")


def _loader(workspace: Path) -> SkillsLoader:
    # Pass the (patchable) module global so tests can point at a fake builtin dir.
    return SkillsLoader(Path(workspace), builtin_skills_dir=BUILTIN_SKILLS_DIR)


def _today() -> str:
    return _dt.date.today().isoformat()


def _update_md(path: Path, mutate) -> None:
    text = path.read_text(encoding="utf-8")
    data, body = split_frontmatter(text)
    mutate(data)
    path.write_text(join_frontmatter(data, body), encoding="utf-8")


def _durin_blob(text: str) -> dict:
    data, _ = split_frontmatter(text)
    meta = data.get("metadata")
    durin = meta.get("durin") if isinstance(meta, dict) else None
    return durin if isinstance(durin, dict) else {}


def read_mode(workspace: Path, name: str, loader: SkillsLoader | None = None) -> str:
    """Explicit metadata.durin.mode, else default by origin (builtin=auto, user=manual)."""
    loader = loader or _loader(workspace)
    text = loader.load_skill(name)
    if text is None:
        return "manual"
    mode = _durin_blob(text).get("mode")
    if mode in ("auto", "manual"):
        return mode
    return "manual" if _skill_md(workspace, name).exists() else "auto"


def read_skill_content(workspace: Path, name: str) -> str | None:
    return _loader(workspace).load_skill(name)


def list_skills_info(workspace: Path) -> list[dict]:
    loader = _loader(workspace)
    out: list[dict] = []
    for entry in loader.list_skills(filter_unavailable=False):
        name = entry["name"]
        text = loader.load_skill(name) or ""
        data, _ = split_frontmatter(text)
        durin = _durin_blob(text)
        prov = durin.get("provenance")
        out.append({
            "name": name,
            "source": entry["source"],
            "mode": read_mode(workspace, name, loader),
            "description": data.get("description", ""),
            "provenance": prov if isinstance(prov, dict) else {},
        })
    return out


def fork_on_write(workspace: Path, name: str, loader: SkillsLoader | None = None) -> Path:
    """Ensure a writable workspace copy of `name`. Copies a builtin in, stamping
    provenance + an explicit mode=auto. Returns the workspace skill dir."""
    loader = loader or _loader(workspace)
    dest = _skills_dir(workspace) / name
    if (dest / "SKILL.md").exists():
        return dest
    src = (loader.builtin_skills or BUILTIN_SKILLS_DIR) / name
    if not (src / "SKILL.md").exists():
        raise FileNotFoundError(f"skill not found: {name}")
    shutil.copytree(src, dest)

    def _stamp(data: dict) -> None:
        durin = ensure_durin(data)
        durin.setdefault("mode", "auto")
        durin["provenance"] = {"source": f"builtin:{name}", "created_at": _today()}

    _update_md(dest / "SKILL.md", _stamp)
    return dest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skills_store_read.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_store.py tests/agent/test_skills_store_read.py
git commit -m "feat(skills): service reads + fork-on-write with provenance"
```

---

## Task 4: skills_store service — set_mode / apply_skill_edit / save_skill_content

**Files:**
- Modify: `durin/agent/skills_store.py`
- Test: `tests/agent/test_skills_store_write.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skills_store_write.py
from pathlib import Path

from durin.agent import skills_store as ss


def _user_skill(ws: Path, name: str, body: str = "Body\n") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n{body}", encoding="utf-8"
    )


def test_set_mode_writes_frontmatter_and_commits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    sha = ss.set_mode(ws, "mine", "auto")
    assert sha is not None
    assert ss.read_mode(ws, "mine") == "auto"


def test_apply_edit_auto_skill_commits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step one\n")
    ss.set_mode(ws, "mine", "auto")
    res = ss.apply_skill_edit(
        ws, "mine", old="step one", new="step ONE (better)", rationale="clarify step",
    )
    assert res["ok"] is True
    assert res["commit"]
    assert "step ONE (better)" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_apply_edit_manual_without_confirm_proposes_only(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step one\n")
    res = ss.apply_skill_edit(ws, "mine", old="step one", new="x", rationale="r")
    assert res.get("proposed") is True
    assert "preview" in res
    assert "step one" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_apply_edit_manual_with_confirm_writes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step one\n")
    res = ss.apply_skill_edit(ws, "mine", old="step one", new="step two", rationale="r", confirm=True)
    assert res["ok"] is True
    assert "step two" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_apply_edit_rejects_missing_rationale_and_bad_match(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    assert "error" in ss.apply_skill_edit(ws, "mine", old="x", new="y", rationale="  ")
    assert "error" in ss.apply_skill_edit(ws, "mine", old="NOPE", new="y", rationale="r", confirm=True)


def test_save_skill_content_requires_manual(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    ok = ss.save_skill_content(ws, "mine", "---\nname: mine\ndescription: d\n---\nNEW\n")
    assert ok["ok"] is True
    ss.set_mode(ws, "mine", "auto")
    rej = ss.save_skill_content(ws, "mine", "whatever")
    assert "error" in rej
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skills_store_write.py -q`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Append the implementation**

```python
# append to durin/agent/skills_store.py

def set_mode(workspace: Path, name: str, mode: str) -> str | None:
    if mode not in ("auto", "manual"):
        raise ValueError("mode must be 'auto' or 'manual'")
    dest = fork_on_write(workspace, name)  # builtins fork so frontmatter persists in workspace
    _update_md(dest / "SKILL.md", lambda d: ensure_durin(d).__setitem__("mode", mode))
    return _store(workspace).auto_commit(f"skill({name}): set mode={mode}")


def _preview(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="before", tofile="after",
    ))


def apply_skill_edit(
    workspace: Path, name: str, *, old: str, new: str, rationale: str,
    file: str = "SKILL.md", confirm: bool = False,
) -> dict:
    """The skill_edit operation: fork-on-write, mode gate, bounded replace, commit."""
    if not rationale or not rationale.strip():
        return {"error": "rationale is required"}
    loader = _loader(workspace)
    if loader.load_skill(name) is None:
        return {"error": f"skill not found: {name}"}
    mode = read_mode(workspace, name, loader)
    dest = fork_on_write(workspace, name, loader)
    target = dest / file
    if not target.exists():
        if old == "":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        else:
            return {"error": f"file not found: {file}"}
    content = target.read_text(encoding="utf-8")
    if old == "":
        updated = content + new
    else:
        n = content.count(old)
        if n == 0:
            return {"error": "old text not found"}
        if n > 1:
            return {"error": "old text not unique"}
        updated = content.replace(old, new, 1)

    if mode == "manual" and not confirm:
        return {
            "proposed": True, "mode": "manual", "name": name, "file": file,
            "note": "skill is manual; re-call with confirm=true after the user approves",
            "preview": _preview(content, updated),
        }
    target.write_text(updated, encoding="utf-8")
    sha = _store(workspace).auto_commit(f"skill({name}): {rationale.strip()}")
    return {"ok": True, "name": name, "file": file, "mode": mode, "commit": sha}


def save_skill_content(workspace: Path, name: str, content: str,
                       rationale: str = "edit via web") -> dict:
    """Full-content overwrite of a MANUAL skill's SKILL.md (web edit surface)."""
    if read_mode(workspace, name) != "manual":
        return {"error": "skill is not manual; flip it to manual to edit"}
    dest = fork_on_write(workspace, name)
    (dest / "SKILL.md").write_text(content, encoding="utf-8")
    sha = _store(workspace).auto_commit(f"skill({name}): {rationale}")
    return {"ok": True, "name": name, "commit": sha}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skills_store_write.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_store.py tests/agent/test_skills_store_write.py
git commit -m "feat(skills): set_mode, apply_skill_edit (mode gate), save_skill_content"
```

---

# MILESTONE B — Adapters

## Task 5: `skill_edit` tool

**Files:**
- Create: `durin/agent/tools/skill_edit.py`
- Test: `tests/agent/tools/test_skill_edit.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/tools/test_skill_edit.py
import asyncio
from pathlib import Path

from durin.agent.tools.skill_edit import SkillEditTool, _PARAMETERS


def _user_skill(ws: Path, name: str, body: str, mode: str = "auto") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  durin:\n    mode: {mode}\n---\n{body}",
        encoding="utf-8",
    )


def test_schema_requires_core_params():
    props = _PARAMETERS["properties"]
    for p in ("name", "old", "new", "rationale"):
        assert p in props
    assert set(_PARAMETERS["required"]) >= {"name", "old", "new", "rationale"}


def test_execute_edits_an_auto_skill(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="auto")
    tool = SkillEditTool(workspace=ws)
    out = asyncio.run(tool.execute(name="mine", old="step one", new="step two", rationale="clarify"))
    assert out["ok"] is True
    assert "step two" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_execute_manual_skill_proposes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="manual")
    tool = SkillEditTool(workspace=ws)
    out = asyncio.run(tool.execute(name="mine", old="step one", new="x", rationale="r"))
    assert out.get("proposed") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/tools/test_skill_edit.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# durin/agent/tools/skill_edit.py
"""skill_edit tool — evolve a skill in-loop with a versioned, rationale'd edit."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the skill to edit (directory name)."),
    old=StringSchema(
        "Exact text to replace. Must be unique in the file. Use an empty "
        "string to append `new` to the end (or create the file)."
    ),
    new=StringSchema("Replacement text."),
    rationale=StringSchema(
        "Why this change improves the skill — recorded as the commit message. "
        "Prefer native durin tools over generic ones and automate repetition."
    ),
    file=StringSchema(
        "File within the skill dir to edit. Defaults to 'SKILL.md'. May target "
        "a script under scripts/."
    ),
    confirm=BooleanSchema(
        description="Required true to apply an edit to a skill in `manual` mode "
        "(after the user approves the proposed diff)."
    ),
    required=["name", "old", "new", "rationale"],
    description=(
        "Edit one of durin's own skills and version the change (reversible). "
        "Use when, mid-task, you discover a better approach than a skill "
        "describes, or a skill has a bug/pitfall worth recording. Editing a "
        "builtin forks it into the workspace first; editing a `manual` skill "
        "returns a proposed diff that needs the user's confirmation."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillEditTool(Tool):
    """skill_edit tool."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "skill_edit"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "Tool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_store import apply_skill_edit

        name = str(kwargs.get("name", "")).strip()
        if not name:
            return {"error": "name is required"}
        return apply_skill_edit(
            self._workspace,
            name,
            old=str(kwargs.get("old", "")),
            new=str(kwargs.get("new", "")),
            rationale=str(kwargs.get("rationale", "")),
            file=str(kwargs.get("file") or "SKILL.md"),
            confirm=bool(kwargs.get("confirm", False)),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/tools/test_skill_edit.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Verify it instantiates (auto-discovery needs no abstract methods left)**

Run: `python -c "from durin.agent.tools.skill_edit import SkillEditTool; print(SkillEditTool('/tmp').name)"`
Expected: prints `skill_edit`.

- [ ] **Step 6: Commit**

```bash
git add durin/agent/tools/skill_edit.py tests/agent/tools/test_skill_edit.py
git commit -m "feat(skills): skill_edit tool (auto-discovered adapter over skills_store)"
```

---

## Task 6: `/skills` command

**Files:**
- Modify: `durin/command/builtin.py` (handler near `cmd_memory`; registration at the `router.exact("/memory"...)` block ~line 1924; palette spec in `BUILTIN_COMMAND_SPECS` ~line 191)
- Test: `tests/command/test_skills_command.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/command/test_skills_command.py
import asyncio
from pathlib import Path
from types import SimpleNamespace

from durin.command.builtin import cmd_skills
from durin.command.router import CommandContext


def _ctx(workspace: Path, args: str) -> CommandContext:
    msg = SimpleNamespace(channel="test", chat_id="c1", metadata={})
    loop = SimpleNamespace(workspace=workspace)
    return CommandContext(msg=msg, session=None, key="/skills", raw=f"/skills {args}",
                          args=args, loop=loop)


def _user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\nBody\n", encoding="utf-8"
    )


def test_skills_list_shows_skills_with_mode(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    out = asyncio.run(cmd_skills(_ctx(ws, "list")))
    assert "mine" in out.content
    assert "manual" in out.content.lower()


def test_skills_mode_sets_and_reports(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    out = asyncio.run(cmd_skills(_ctx(ws, "mode mine auto")))
    assert "auto" in out.content.lower()
    from durin.agent.skills_store import read_mode
    assert read_mode(ws, "mine") == "auto"


def test_skills_mode_usage_on_bad_args(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = asyncio.run(cmd_skills(_ctx(ws, "mode")))
    assert "usage" in out.content.lower()
```

> The handler only reads `msg.channel`, `msg.chat_id`, `msg.metadata`, so `SimpleNamespace` suffices — no `InboundMessage` import needed.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/command/test_skills_command.py -q`
Expected: FAIL — `cannot import name 'cmd_skills'`.

- [ ] **Step 3: Add the handler** (next to `cmd_memory` in `durin/command/builtin.py`)

```python
async def cmd_skills(ctx: CommandContext) -> OutboundMessage:
    """Skill operations dispatcher: list, mode."""
    from durin.agent import skills_store as ss

    workspace = _resolve_workspace(ctx.loop)
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    parts = (ctx.args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    def _reply(content: str) -> OutboundMessage:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=content, metadata=metadata_text,
        )

    if not sub or sub == "list":
        skills = ss.list_skills_info(workspace)
        if not skills:
            return _reply("No skills found.")
        lines = ["## Skills", ""]
        for s in skills:
            lines.append(
                f"- **{s['name']}** [{s['source']}] · mode=`{s['mode']}` — {s['description']}"
            )
        return _reply("\n".join(lines))

    if sub == "mode":
        mparts = rest.split()
        if len(mparts) != 2 or mparts[1].lower() not in {"auto", "manual"}:
            return _reply("Usage: `/skills mode <name> <auto|manual>`")
        name, want = mparts[0], mparts[1].lower()
        try:
            sha = ss.set_mode(workspace, name, want)
        except FileNotFoundError:
            return _reply(f"Skill `{name}` not found.")
        suffix = f" ({sha})" if sha else ""
        return _reply(f"Skill `{name}` mode → **{want}**{suffix}.")

    return _reply(f"Unknown `/skills` subcommand `{sub}`. Try `list` or `mode`.")
```

- [ ] **Step 4: Register it** — add next to the `/memory` registration:

```python
    router.exact("/skills", cmd_skills)
    router.prefix("/skills ", cmd_skills)
```

And add a palette spec to `BUILTIN_COMMAND_SPECS` (mirror the `/memory` spec):

```python
    BuiltinCommandSpec(
        "/skills",
        "Skill operations",
        "Subcommands: list, mode <name> <auto|manual>.",
        "wrench",
        "<list|mode> [args]",
    ),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/command/test_skills_command.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add durin/command/builtin.py tests/command/test_skills_command.py
git commit -m "feat(skills): /skills command (list + mode)"
```

---

## Task 7: Web API routes (GET-only, in the websockets channel)

**Files:**
- Modify: `durin/agent/skills_store.py` (add `(status, payload)` web helpers — pure, testable)
- Modify: `durin/channels/websocket.py` (4 routes in `_dispatch_http` + 4 thin handlers)
- Test: `tests/agent/test_skills_store_web.py`

- [ ] **Step 1: Write the failing test** (against the pure web helpers — no channel needed)

```python
# tests/agent/test_skills_store_web.py
from pathlib import Path

from durin.agent import skills_store as ss


def _user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\nBody\n", encoding="utf-8"
    )


def test_web_list_returns_200_and_skills(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    status, payload = ss.web_list(ws)
    assert status == 200
    assert any(s["name"] == "mine" for s in payload["skills"])


def test_web_get_returns_content_or_404(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    status, payload = ss.web_get(ws, "mine")
    assert status == 200 and "Body" in payload["content"]
    assert ss.web_get(ws, "nope")[0] == 404


def test_web_mode_sets_and_validates(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    assert ss.web_mode(ws, "mine", "auto")[0] == 200
    assert ss.read_mode(ws, "mine") == "auto"
    assert ss.web_mode(ws, "mine", "bogus")[0] == 400


def test_web_save_requires_manual(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    assert ss.web_save(ws, "mine", "---\nname: mine\ndescription: d\n---\nNEW\n")[0] == 200
    ss.web_mode(ws, "mine", "auto")
    assert ss.web_save(ws, "mine", "x")[0] == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skills_store_web.py -q`
Expected: FAIL — `web_list` etc. not defined.

- [ ] **Step 3: Append the web helpers to `durin/agent/skills_store.py`**

```python
# append to durin/agent/skills_store.py

def web_list(workspace: Path) -> tuple[int, dict]:
    head = _store(workspace).log(max_entries=1)
    return 200, {
        "skills": list_skills_info(workspace),
        "store_head": ({"sha": head[0].sha, "at": head[0].timestamp} if head else None),
    }


def web_get(workspace: Path, name: str) -> tuple[int, dict]:
    content = read_skill_content(workspace, name)
    if content is None:
        return 404, {"error": f"skill not found: {name}"}
    return 200, {"name": name, "mode": read_mode(workspace, name), "content": content}


def web_save(workspace: Path, name: str, content: str) -> tuple[int, dict]:
    res = save_skill_content(workspace, name, content)
    return (400, res) if "error" in res else (200, res)


def web_mode(workspace: Path, name: str, value: str) -> tuple[int, dict]:
    if value not in ("auto", "manual"):
        return 400, {"error": "value must be 'auto' or 'manual'"}
    try:
        sha = set_mode(workspace, name, value)
    except FileNotFoundError:
        return 404, {"error": f"skill not found: {name}"}
    return 200, {"ok": True, "name": name, "mode": value, "commit": sha}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skills_store_web.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire the routes into `durin/channels/websocket.py`**

In `_dispatch_http` (REST handler block, after the existing `re.match(...)` routes ~line 717), add — **`/save` and `/mode` patterns BEFORE the bare name pattern**:

```python
        if got == "/api/skills":
            return self._handle_skills_list(request)

        m = re.match(r"^/api/skills/([^/]+)/save$", got)
        if m:
            return self._handle_skill_save(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/mode$", got)
        if m:
            return self._handle_skill_mode(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)$", got)
        if m:
            return self._handle_skill_get(request, m.group(1))
```

Add the four thin handlers (next to `_handle_cron_list` — auth → load workspace → call service → `_http_json_response`):

```python
    def _handle_skills_list(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_list(workspace)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skills list failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skill_get(self, request: WsRequest, name: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_get(workspace, _decode_api_key(name))
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skill read failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skill_save(self, request: WsRequest, name: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        query = _parse_query(request.path)
        content = _query_first(query, "content")
        if content is None:
            return _http_error(400, "content is required")
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_save(workspace, _decode_api_key(name), content)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skill save failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skill_mode(self, request: WsRequest, name: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        query = _parse_query(request.path)
        value = (_query_first(query, "value") or "").strip()
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_mode(workspace, _decode_api_key(name), value)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skill mode failed: {exc}")
        return _http_json_response(payload, status=status)
```

- [ ] **Step 6: Smoke-test the channel imports cleanly**

Run: `python -c "import durin.channels.websocket"`
Expected: no error.

- [ ] **Step 7: Commit**

```bash
git add durin/agent/skills_store.py durin/channels/websocket.py tests/agent/test_skills_store_web.py
git commit -m "feat(skills): GET-only web routes (list/get/save/mode) + service glue"
```

---

# MILESTONE C — WebUI panel

## Task 8: api.ts client functions

**Files:**
- Modify: `webui/src/lib/api.ts` (add near the cron block ~line 231)
- Test: `webui/src/tests/skills-api.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// webui/src/tests/skills-api.test.ts
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
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `webui/`): `bun run test -- skills-api`
Expected: FAIL — `listSkills` not exported.

- [ ] **Step 3: Add to `webui/src/lib/api.ts`**

```ts
export interface SkillRow {
  name: string;
  source: string;
  mode: "auto" | "manual";
  description?: string;
  provenance?: { source?: string; created_at?: string };
}

export interface SkillDetail {
  name: string;
  mode: "auto" | "manual";
  content: string;
}

export async function listSkills(token: string, base = ""): Promise<SkillRow[]> {
  const res = await request<{ skills: SkillRow[] }>(`${base}/api/skills`, token);
  return res.skills;
}

export async function getSkill(token: string, name: string, base = ""): Promise<SkillDetail> {
  return request<SkillDetail>(`${base}/api/skills/${encodeURIComponent(name)}`, token);
}

export async function saveSkill(token: string, name: string, content: string, base = ""): Promise<void> {
  const query = new URLSearchParams({ content });
  await request<{ ok: boolean }>(`${base}/api/skills/${encodeURIComponent(name)}/save?${query}`, token);
}

export async function setSkillMode(
  token: string, name: string, value: "auto" | "manual", base = "",
): Promise<void> {
  const query = new URLSearchParams({ value });
  await request<{ ok: boolean }>(`${base}/api/skills/${encodeURIComponent(name)}/mode?${query}`, token);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `webui/`): `bun run test -- skills-api`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add webui/src/lib/api.ts webui/src/tests/skills-api.test.ts
git commit -m "feat(webui): skills api client (list/get/save/mode)"
```

---

## Task 9: SkillsSettings panel + tab registration

**Files:**
- Create: `webui/src/components/settings/SkillsSettings.tsx`
- Modify: `webui/src/components/settings/SettingsView.tsx` (4 edit sites)
- Modify: `webui/src/i18n/locales/en.json` (+ other locales) — add `settings.nav.skills`

- [ ] **Step 1: Create the panel** `webui/src/components/settings/SkillsSettings.tsx`

```tsx
import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  ApiError,
  getSkill,
  listSkills,
  saveSkill,
  setSkillMode,
  type SkillDetail,
  type SkillRow,
} from "@/lib/api";
import { SettingsGroup, SettingsRow, SettingsSectionTitle } from "./primitives";

export function SkillsSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<SkillRow[] | null>(null);
  const [selected, setSelected] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRows(await listSkills(token));
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const open = useCallback(
    async (name: string) => {
      setError(null);
      try {
        const detail = await getSkill(token, name);
        setSelected(detail);
        setDraft(detail.content);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [token],
  );

  const toggleMode = useCallback(
    async (row: SkillRow) => {
      setBusy(row.name);
      try {
        await setSkillMode(token, row.name, row.mode === "auto" ? "manual" : "auto");
        await refresh();
        if (selected?.name === row.name) await open(row.name);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [token, refresh, selected, open],
  );

  const save = useCallback(async () => {
    if (!selected) return;
    setBusy(selected.name);
    try {
      await saveSkill(token, selected.name, draft);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }, [token, selected, draft, refresh]);

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        {t("settings.nav.skills")}
      </div>
    );
  }

  return (
    <SettingsGroup>
      <SettingsSectionTitle>{t("settings.nav.skills")}</SettingsSectionTitle>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {(rows ?? []).map((row) => (
        <SettingsRow
          key={row.name}
          title={`${row.name}  ·  ${row.mode}`}
          description={`${row.source}${row.description ? ` — ${row.description}` : ""}`}
        >
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => void open(row.name)}>
              View
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={busy === row.name}
              onClick={() => void toggleMode(row)}
            >
              {row.mode === "auto" ? "Make manual" : "Make auto"}
            </Button>
          </div>
        </SettingsRow>
      ))}

      {selected && (
        <div className="mt-4 space-y-2">
          <p className="text-sm font-medium">
            {selected.name} ({selected.mode})
          </p>
          <textarea
            className="h-64 w-full rounded-md border bg-background p-2 font-mono text-xs"
            value={draft}
            disabled={selected.mode !== "manual"}
            onChange={(e) => setDraft(e.target.value)}
          />
          {selected.mode !== "manual" ? (
            <p className="text-xs text-muted-foreground">
              Read-only: this skill is in <code>auto</code> mode (managed by the agent). Switch it to{" "}
              <code>manual</code> to edit.
            </p>
          ) : (
            <Button size="sm" disabled={busy === selected.name} onClick={() => void save()}>
              Save
            </Button>
          )}
        </div>
      )}
    </SettingsGroup>
  );
}
```

> The panel receives `token` as a prop (same as the sibling settings panels) — no token hook import needed.

- [ ] **Step 2: Register the tab in `webui/src/components/settings/SettingsView.tsx`** (4 edit sites)

(a) Import (~line 63): `import { SkillsSettings } from "@/components/settings/SkillsSettings";`
(b) Add `| "skills"` to the `SettingsSectionKey` union (~line 78).
(c) Add a nav item in `SETTINGS_NAV_ITEMS` (~line 464; `Sparkles` is already imported ~line 32): `{ key: "skills", icon: Sparkles },`
(d) Add the render branch (~line 406, next to `memory`):
```tsx
              ) : activeSection === "skills" ? (
                <SkillsSettings token={token} />
```

- [ ] **Step 3: Add the i18n key** in `webui/src/i18n/locales/en.json` (mirror in other locale files) under `settings.nav`: `"skills": "Skills"`

- [ ] **Step 4: Lint, typecheck, build**

Run (from `webui/`):
```
bun run lint
bun run test
bun run build
```
Expected: lint clean, vitest passes (incl. Task 8), `tsc` + `vite build` succeed.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/settings/SkillsSettings.tsx webui/src/components/settings/SettingsView.tsx webui/src/i18n/locales
git commit -m "feat(webui): SkillsSettings panel (list/view/edit-manual/toggle)"
```

---

# Final verification

- [ ] **Run the full backend suite**

Run: `pytest tests/ -q --maxfail=5`
Expected: PASS (no regressions; new tests green).

- [ ] **Live check (per the project's "verify live" discipline)**

1. Fresh workspace: confirm `workspace/skills/.git` exists right after setup (eager init).
2. In a session: ask the agent to improve a builtin skill → a workspace fork appears under `workspace/skills/<name>/` and `git -C workspace/skills log` shows a rationale'd commit.
3. `/skills list` shows modes; `/skills mode <name> manual` flips it.
4. Web dashboard → Skills tab: a `manual` skill is editable + saves; an `auto` skill is read-only; toggle works.
5. Revert: `GitStore(workspace/'skills', subtree=True, label='skills').revert(sha)` restores a prior version.

> Reversibility is exposed programmatically via `GitStore.revert(sha)` (subtree mode); a `/skills revert <sha>` surface is a fast-follow (not in this plan).

---

## Self-review notes (coverage vs spec)

- Spec §2 versioning → Tasks 1 + 1b. §3/§3.5 frontmatter + open-standard namespacing (`metadata.durin.*`) → Task 2. §5 tool (fork-on-write, mode gate, rationale commit, scripts via `file`) → Tasks 3–5. §6 modes + defaults (builtin=auto, user=manual, default-in-read) → Tasks 3–4. §7.5 web (list/view/edit-manual/toggle, auto=read-only) → Tasks 7–9. §5 command → Task 6. §9 error handling (missing rationale, non-unique/absent match, git best-effort) → Tasks 1/4. §10 tests → every task.
- **Deferred (as designed):** auto soft-gate, crystallize (E2), import/three-layer/security-floor (E3), skills-in-search retrieval (E4), `/skills revert` surface, per-skill last-commit in the web list (uses `store_head` instead).
