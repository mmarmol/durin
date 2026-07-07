"""Service layer for durin's skill versioning + mode system.

All skill mutations go through here so the tool, the /skills command, and the
web routes share one implementation (and one git store). Pure functions over a
workspace Path — directly unit-testable with tmp_path.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from durin.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader
from durin.agent.skills_frontmatter import ensure_durin, join_frontmatter, split_frontmatter
from durin.utils.gitstore import GitStore

logger = logging.getLogger(__name__)

# Bump this constant whenever a curation rule is added to skill_curation.md.
# `needs_curation` will re-check skills with stale rules versions, pulling them
# back through the curation gate once per rules update cycle.
# v3: composition doctrine — narration-only skills evolve into workflow-delegating
# wrappers.
# v4: full composition repair — inline deterministic code is lifted into a
# bundled script, and a missing workflow is authored so the skill can delegate
# (curation `restructure` action; pre-doctrine skills re-enter the delta to be
# repaired now that the vocabulary can express it).
CURATION_RULES_VERSION = 4


@dataclass
class Attribution:
    """Who/what produced a skill mutation, stamped as git commit trailers.

    `actor` is one of "user" | "agent" | "curation" | "import". `session` and
    `agent` (model name) are optional and omitted when unknown.
    """
    actor: str
    session: str | None = None
    agent: str | None = None


def attribution_to_trailers(attr: "Attribution | None") -> dict[str, str]:
    """Render an Attribution as `{Actor, Session, Agent}` trailers (present keys only)."""
    if attr is None:
        return {}
    out: dict[str, str] = {}
    for key, val in (("Actor", attr.actor), ("Session", attr.session), ("Agent", attr.agent)):
        if val is not None and str(val) != "":
            out[key] = str(val)
    return out


def _skills_dir(workspace: Path) -> Path:
    return Path(workspace) / "skills"


def _skill_md(workspace: Path, name: str) -> Path:
    return _skills_dir(workspace) / name / "SKILL.md"


def _store(workspace: Path) -> GitStore:
    return GitStore(_skills_dir(workspace), subtree=True, label="skills")


def _safe_name(name: str) -> bool:
    """Reject skill names that could escape the skills dir (path traversal)."""
    return bool(name) and name not in (".", "..") and not any(
        c in name for c in ("/", "\\", "\x00")
    )


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


_TRIGGER_HEADING = re.compile(r"^##\s*Triggers?\b.*$", re.IGNORECASE | re.MULTILINE)


def _frontmatter_description(content: str) -> str:
    """Read `description` from the raw frontmatter, if any (reuses the
    module's existing frontmatter reader instead of a second YAML parser)."""
    data, _body = split_frontmatter(content)
    return str(data.get("description") or "").strip()


def _derive_description(body: str) -> str:
    """Description from the body: first prose paragraph after the H1 plus a
    collapsed Triggers section, capped. Empty string when nothing usable."""
    text = body
    m = re.match(r"^---\n.*?\n---\n?", text, re.DOTALL)
    if m:
        text = text[m.end():]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text)]
    prose = next((p for p in paras
                  if p and not p.startswith("#") and not p.startswith("---")), "")
    # Collapse internal newlines: wrapped markdown paragraphs must not land
    # verbatim in the frontmatter, or a later exact-match edit targeting that
    # paragraph would find two occurrences and fail the uniqueness check.
    prose = " ".join(prose.split())
    trig = ""
    tm = _TRIGGER_HEADING.search(text)
    if tm:
        tail = text[tm.end():]
        nxt = re.search(r"^#{1,6}\s", tail, re.MULTILINE)
        section = tail[: nxt.start()] if nxt else tail
        trig = " ".join(line.strip("-* \t") for line in section.splitlines()
                        if line.strip()).strip()
    out = " ".join(x for x in (prose, ("Triggers: " + trig) if trig else "") if x)
    return out[:500].strip()


def _ensure_surface_frontmatter(md: Path, name: str) -> None:
    """Guarantee the frontmatter carries name + description — the only fields
    the agent reads to decide when a skill applies. Derive from the body when
    the author omitted them (the prompt-summary fallback is just the name,
    which makes the skill invisible)."""
    text = md.read_text(encoding="utf-8")

    def _fill(data: dict) -> None:
        if not data.get("name"):
            data["name"] = name
        if not data.get("description"):
            data["description"] = _derive_description(text)

    _update_md(md, _fill)


def _body_hash(text: str) -> str:
    _data, body = split_frontmatter(text)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _resolve_skill_dir(workspace: Path, name: str) -> Path | None:
    """The directory holding the skill: the workspace copy if forked, else the
    builtin package dir. None for an unsafe/unknown name."""
    if not _safe_name(name):
        return None
    ws = _skills_dir(workspace) / name
    if (ws / "SKILL.md").exists():
        return ws
    builtin = (_loader(workspace).builtin_skills or BUILTIN_SKILLS_DIR) / name
    if (builtin / "SKILL.md").exists():
        return builtin
    return None


def _is_text_bytes(raw: bytes) -> bool:
    """Decode-probe: not text if it has a NUL byte or fails UTF-8."""
    if b"\x00" in raw:
        return False
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def skill_files(workspace: Path, name: str) -> list[dict]:
    """Flat list of a skill's files: [{path, text, size}], sorted by path.
    Skips hidden entries (any dotfile or dot-directory, at any depth) and build
    junk (``__pycache__``). Returns [] for an unsafe/unknown name."""
    root = _resolve_skill_dir(workspace, name)
    if root is None:
        return []
    out: list[dict] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        raw = p.read_bytes()[:65_536]
        out.append({"path": str(rel), "text": _is_text_bytes(raw), "size": p.stat().st_size})
    return out


def _safe_target(root: Path, relpath: str) -> Path | None:
    """Resolve `relpath` under `root`, rejecting escapes. None if it escapes.
    Rejects absolute paths and any path containing '..' segments."""
    p = Path(relpath)
    if p.is_absolute() or ".." in p.parts:
        return None
    target = (root / relpath).resolve()
    if not target.is_relative_to(root.resolve()):
        return None
    return target


def read_skill_file(workspace: Path, name: str, relpath: str) -> dict | None:
    """Read one file. Returns {path, text, content} (content="" for binary),
    or None for unsafe/unknown skill, traversal, or a missing file."""
    root = _resolve_skill_dir(workspace, name)
    if root is None:
        return None
    target = _safe_target(root, relpath)
    if target is None or not target.is_file():
        return None
    raw = target.read_bytes()
    if not _is_text_bytes(raw[:65_536]):
        return {"path": relpath, "text": False, "content": ""}
    return {"path": relpath, "text": True, "content": raw.decode("utf-8")}


def _lint_script(relpath: str, content: str) -> dict | None:
    """Blocking syntax lint for scripts and config files. Returns an error dict
    on failure, else None. In-process parsers (no subprocess, no new deps):
    .py -> compile(); .json -> json.loads(); .toml -> tomllib.loads();
    .yaml/.yml -> yaml.safe_load_all(). .sh -> `bash -n`. Unknown extensions /
    missing bash -> no lint (None)."""
    suffix = Path(relpath).suffix.lower()
    if suffix == ".py":
        try:
            compile(content, relpath, "exec")
            return None
        except SyntaxError as exc:
            return {"error": "syntax", "lang": "python",
                    "detail": exc.msg or "syntax error", "line": exc.lineno or 0}
    if suffix == ".json":
        try:
            json.loads(content)
            return None
        except json.JSONDecodeError as exc:
            return {"error": "syntax", "lang": "json",
                    "detail": exc.msg or "invalid JSON", "line": exc.lineno or 0}
    if suffix == ".toml":
        try:
            tomllib.loads(content)
            return None
        except tomllib.TOMLDecodeError as exc:
            return {"error": "syntax", "lang": "toml",
                    "detail": str(exc) or "invalid TOML", "line": 0}
    if suffix in (".yaml", ".yml"):
        try:
            list(yaml.safe_load_all(content))
            return None
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = (mark.line + 1) if mark is not None else 0
            detail = getattr(exc, "problem", None) or str(exc) or "invalid YAML"
            return {"error": "syntax", "lang": "yaml", "detail": detail, "line": line}
    if suffix == ".sh":
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as fh:
                fh.write(content)
                tmp = fh.name
        except OSError:
            return None
        try:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}
            proc = subprocess.run(["bash", "-n", tmp], capture_output=True, text=True, env=env, timeout=10)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None  # bash unavailable -> skip lint (best-effort)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if proc.returncode != 0:
            return {"error": "syntax", "lang": "bash",
                    "detail": (proc.stderr or "syntax error").strip(), "line": 0}
    return None


def _skill_md_integrity(content: str) -> str | None:
    """Tier-1 integrity floor for a whole SKILL.md body: reject a structurally
    broken / truncated body so no author — a webui full-save, an import, or a
    dream authoring pass — can persist an unusable skill. Returns an error
    string, or None when the body is sound.

    Scoped to WHOLE-body writes: a bounded ``apply_skill_edit`` (single-occurrence
    old→new replace on an already-valid file) does NOT pass through here, because
    it cannot turn a valid SKILL.md into a broken one."""
    if not content.strip():
        return "SKILL.md body is empty"
    if not (_frontmatter_description(content) or _derive_description(content)):
        return "SKILL.md has no derivable description (truncated or malformed body)"
    return None


def save_skill_file(workspace: Path, name: str, relpath: str, content: str, *,
                    rationale: str = "edit via web",
                    attribution: "Attribution | None" = None) -> dict:
    """Save one text file in a skill: fork-on-write, script lint (blocking),
    write, commit (with attribution trailers), security re-scan (non-blocking).

    Editable in either mode. `manual` means "the user owns this skill"; `auto`
    means "dream may auto-improve it" — neither locks the user out of editing.
    A user edit to an `auto` skill is committed with the user's attribution and
    left `auto`, so dream keeps curating it (respecting the edit, not reverting
    it blindly)."""
    if not _safe_name(name):
        return {"error": "invalid skill name"}
    lint = _lint_script(relpath, content)
    if lint is not None:
        return lint  # blocked - nothing written
    if relpath == "SKILL.md":
        bad = _skill_md_integrity(content)
        if bad is not None:
            return {"error": bad}  # integrity floor - nothing written
    store = _store_init(workspace)
    dest = fork_on_write(workspace, name)
    target = _safe_target(dest, relpath)
    if target is None:
        return {"error": "file escapes skill directory"}
    if target.exists() and target.is_dir():
        return {"error": "path is a directory"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    sha = store.auto_commit(f"skill({name}): {rationale}",
                            trailers=attribution_to_trailers(attribution))
    _sync_index(workspace, name)
    payload = {"ok": True, "name": name, "path": relpath, "commit": sha}
    # Non-blocking security re-scan so the UI can refresh the verdict badge.
    try:
        from durin.security.skill_scan import scan_skill
        rep = scan_skill(dest)
        payload["verdict"] = rep.verdict
        payload["findings"] = [{"category": f.category, "severity": f.severity,
                                "where": f.where, "detail": f.detail} for f in rep.findings]
    except Exception as exc:  # noqa: BLE001 - scan is advisory, never fatal
        logger.warning("post-save scan failed for %s: %s", name, exc)
    return payload


def _index_skills_enabled() -> bool:
    """Whether skill-memory-class indexing is configured on.

    Delegates to the memory-layer single source of truth
    (:func:`durin.memory.index_meta.skills_indexing_enabled`) so the
    write-side gate here and the read-side gates in the indexer / vector
    index / search all consult the same best-effort logic. Best-effort: a
    missing/unloadable config (pure tmp_path unit tests) is treated as
    enabled; only an explicit false bails early.
    """
    from durin.memory.index_meta import skills_indexing_enabled

    return skills_indexing_enabled()


def _vector_index_for(workspace: Path):
    """Construct a :class:`VectorIndex` the way the rest of the codebase does
    (``FastembedProvider(model=config.memory.embedding.model)``), or ``None``
    when the optional lancedb extra is absent. Loading the embedding model is
    heavy but only happens when lancedb is installed — guarded so pure
    tmp_path unit tests stay fast (lancedb absent → no-op)."""
    from durin.memory.vector_index import VectorIndex, vector_index_available

    if not vector_index_available():
        return None
    from durin.config.loader import load_config
    from durin.memory.embedding import FastembedProvider

    cfg = load_config()
    provider = FastembedProvider(model=cfg.memory.embedding.model)
    return VectorIndex(workspace, provider)


def _sync_index(workspace: Path, name: str) -> None:
    """Upsert a skill into the memory index (FTS + vector) after a mutation.

    No-op when indexing is disabled (``memory.index_skills=false``) or the
    optional lancedb extra is unavailable — which keeps pure tmp_path unit
    tests fast. Failures are logged, never raised: an index drift must not
    break the (already-committed) skill write.
    """
    if not _index_skills_enabled():
        return
    try:
        from durin.memory.indexer import reindex_one_skill
        from durin.memory.paths import skill_dir, skill_path_from_uri, skill_uri
        from durin.memory.skill_page import SkillPage

        skill_md = skill_dir(workspace, name) / "SKILL.md"
        # FTS (cheap, no embedding model).
        reindex_one_skill(workspace, skill_md, trigger="skill_store")
        # Vector (needs the embedding provider; guarded on lancedb).
        vi = _vector_index_for(workspace)
        if vi is not None:
            sp = SkillPage.from_file(skill_md)
            if sp is not None and not sp.disabled:
                vi.upsert_skill(
                    name=sp.name,
                    description=sp.description,
                    body=sp.body,
                    path=skill_path_from_uri(skill_uri(name)),
                    mode=sp.mode,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill index sync failed for %s: %s", name, exc)


def _unsync_index(workspace: Path, name: str) -> None:
    """Evict a removed skill from the memory index (FTS + vector).

    Called when a mutation deletes/rmtrees a workspace skill (fuse sources).
    ``reindex_one_skill`` deletes the FTS row by uri when the file is gone;
    the vector row is dropped by ``delete_by_id(skill_uri(name))``. No-op /
    logged-failure semantics mirror :func:`_sync_index`.
    """
    if not _index_skills_enabled():
        return
    try:
        from durin.memory.indexer import reindex_one_skill
        from durin.memory.paths import skill_dir, skill_uri

        skill_md = skill_dir(workspace, name) / "SKILL.md"
        # File is gone → reindex_one_skill takes the delete-by-uri branch.
        reindex_one_skill(workspace, skill_md, trigger="skill_store")
        vi = _vector_index_for(workspace)
        if vi is not None:
            vi.delete_by_id(skill_uri(name))
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill index unsync failed for %s: %s", name, exc)


def needs_curation(workspace: Path, name: str) -> bool:
    """True when the skill is new or its BODY changed since last curated, or when
    the stored curation rules version is absent or older than the current version."""
    text = read_skill_content(workspace, name)
    if text is None:
        return False
    durin = _durin_blob(text)
    prov = durin.get("provenance")
    stored = prov.get("dream_processed_through") if isinstance(prov, dict) else None
    body_hash_mismatch = stored != _body_hash(text)
    # Re-check if rules version is absent or stale
    stored_rules = durin.get("curation_rules")
    rules_version_stale = stored_rules is None or stored_rules < CURATION_RULES_VERSION
    return body_hash_mismatch or rules_version_stale


def mark_curated(workspace: Path, name: str) -> str | None:
    """Stamp provenance.dream_processed_through = current body hash + curation_rules version + commit."""
    if not _safe_name(name):
        return None
    store = _store_init(workspace)
    dest = fork_on_write(workspace, name)
    h = _body_hash((dest / "SKILL.md").read_text(encoding="utf-8"))

    def _set(data: dict) -> None:
        durin = ensure_durin(data)
        prov = durin.get("provenance")
        if not isinstance(prov, dict):
            prov = {"source": "unknown", "created_at": _today()}
        prov["dream_processed_through"] = h
        durin["provenance"] = prov
        durin["curation_rules"] = CURATION_RULES_VERSION

    _update_md(dest / "SKILL.md", _set)
    sha = store.auto_commit(f"skill({name}): curated @ {h}")
    _sync_index(workspace, name)
    return sha


def backfill_surface_frontmatter(workspace: Path, name: str) -> bool:
    """Deterministically fill a missing name/description in `name`'s
    frontmatter (see `_ensure_surface_frontmatter`) and commit if it changed
    anything. Returns True only when the file was actually modified."""
    if not _safe_name(name):
        return False
    store = _store_init(workspace)
    dest = fork_on_write(workspace, name)
    md = dest / "SKILL.md"
    before = md.read_text(encoding="utf-8")
    _ensure_surface_frontmatter(md, name)
    after = md.read_text(encoding="utf-8")
    if after == before:
        return False
    store.auto_commit(f"skill({name}): backfill frontmatter description [curation]",
                      trailers=attribution_to_trailers(Attribution(actor="curation")))
    _sync_index(workspace, name)
    return True


def read_mode(workspace: Path, name: str, loader: SkillsLoader | None = None) -> str:
    """Explicit metadata.durin.mode, else default by origin (builtin=auto, user=manual)."""
    if not _safe_name(name):
        return "manual"
    loader = loader or _loader(workspace)
    text = loader.load_skill(name)
    if text is None:
        return "manual"
    mode = _durin_blob(text).get("mode")
    if mode in ("auto", "manual"):
        return mode
    return "manual" if _skill_md(workspace, name).exists() else "auto"


def read_skill_content(workspace: Path, name: str) -> str | None:
    if not _safe_name(name):
        return None
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
            "version": data.get("version", ""),
            "license": data.get("license", ""),
            "provenance": prov if isinstance(prov, dict) else {},
        })
    return out


def _store_init(workspace: Path) -> GitStore:
    """Return the skills GitStore, initializing it on first use."""
    store = _store(workspace)
    if not store.is_initialized():
        store.init()
    return store


def fork_on_write(workspace: Path, name: str, loader: SkillsLoader | None = None) -> Path:
    """Ensure a writable workspace copy of `name`. Copies a builtin in, stamping
    provenance + an explicit mode=auto. Returns the workspace skill dir."""
    if not _safe_name(name):
        raise FileNotFoundError(f"invalid skill name: {name}")
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
        durin.setdefault("provenance", {"source": f"builtin:{name}", "created_at": _today()})

    _update_md(dest / "SKILL.md", _stamp)
    return dest


def set_mode(workspace: Path, name: str, mode: str) -> str | None:
    if mode not in ("auto", "manual"):
        raise ValueError("mode must be 'auto' or 'manual'")
    if not _safe_name(name):
        raise FileNotFoundError(f"invalid skill name: {name}")
    store = _store_init(workspace)  # ensure git repo exists before mutating files
    dest = fork_on_write(workspace, name)
    def _set(data: dict) -> None:
        ensure_durin(data)["mode"] = mode

    _update_md(dest / "SKILL.md", _set)
    sha = store.auto_commit(f"skill({name}): set mode={mode}")
    _sync_index(workspace, name)
    return sha


def removable_action(workspace: Path, name: str,
                     loader: SkillsLoader | None = None) -> str | None:
    """Classify whether/how a skill can be removed.

    - "remove": a pure workspace skill (imported / dream / fused) — deleting it
      makes it disappear.
    - "revert": a workspace copy that shadows a builtin of the same name (a fork)
      — deleting the copy restores the shipped builtin.
    - None: a pure builtin (no workspace copy) or an unknown name — nothing to
      remove; the package dir must never be touched.
    """
    if not _safe_name(name):
        return None
    if not _skill_md(workspace, name).exists():
        return None
    loader = loader or _loader(workspace)
    builtin_md = (loader.builtin_skills or BUILTIN_SKILLS_DIR) / name / "SKILL.md"
    return "revert" if builtin_md.exists() else "remove"


def remove_skill(workspace: Path, name: str) -> dict:
    """Delete a workspace skill — the mirror of :func:`install_imported_skill`.

    Removes the workspace ``skills/<name>/`` dir, commits the deletion to the
    skills git store (so it is recoverable), evicts the skill from the memory
    index, and appends an audit entry. Builtins (package) are never touched: a
    forked builtin reverts to the shipped version, a pure builtin is refused.
    """
    if not _safe_name(name):
        return {"error": "invalid skill name"}
    loader = _loader(workspace)
    action = removable_action(workspace, name, loader)
    if action is None:
        if loader.load_skill(name) is None:
            return {"error": f"skill not found: {name}"}
        return {"error": f"builtin skills cannot be removed: {name}"}
    store = _store_init(workspace)
    dest = _skills_dir(workspace) / name
    shutil.rmtree(dest)
    label = "revert to builtin" if action == "revert" else "remove"
    sha = store.auto_commit(f"skill({name}): {label}")
    _unsync_index(workspace, name)
    # Local import avoids a circular import (skills_import imports skills_store).
    from durin.agent.skills_import import _audit
    _audit(workspace, name=name, action="remove", result=action, commit=sha)
    return {"ok": True, "name": name, "action": action, "commit": sha}


def _preview(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="before", tofile="after",
    ))


def apply_skill_edit(
    workspace: Path, name: str, *, old: str, new: str, rationale: str,
    file: str = "SKILL.md", confirm: bool = False,
    attribution: "Attribution | None" = None,
) -> dict:
    """The skill_edit operation: fork-on-write, mode gate, bounded replace, commit."""
    if not rationale or not rationale.strip():
        return {"error": "rationale is required"}
    if not _safe_name(name):
        return {"error": "invalid skill name"}
    loader = _loader(workspace)
    if loader.load_skill(name) is None:
        return {"error": f"skill not found: {name}"}
    mode = read_mode(workspace, name, loader)
    store = _store_init(workspace)  # ensure git repo exists before mutating files
    dest = fork_on_write(workspace, name, loader)
    target = (dest / file).resolve()
    if not target.is_relative_to(dest.resolve()):
        return {"error": "file escapes skill directory"}
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
            return {"error": ("old text not unique — it may appear in both the "
                              "frontmatter description and the body; include "
                              "surrounding lines to pin one occurrence")}
        updated = content.replace(old, new, 1)

    if mode == "manual" and not confirm:
        return {
            "proposed": True, "mode": "manual", "name": name, "file": file,
            "note": "skill is manual; re-call with confirm=true after the user approves",
            "preview": _preview(content, updated),
        }
    target.write_text(updated, encoding="utf-8")
    sha = store.auto_commit(f"skill({name}): {rationale.strip()}",
                            trailers=attribution_to_trailers(attribution))
    _sync_index(workspace, name)
    return {"ok": True, "name": name, "file": file, "mode": mode, "commit": sha}


def save_skill_content(workspace: Path, name: str, content: str,
                       rationale: str = "edit via web",
                       attribution: "Attribution | None" = None) -> dict:
    """Full-content overwrite of a skill's SKILL.md (web edit surface), in
    either mode — see :func:`save_skill_file` for the mode semantics."""
    return save_skill_file(workspace, name, "SKILL.md", content,
                           rationale=rationale, attribution=attribution)


def _safe_bundle_path(rel: str) -> bool:
    """A bundled file path must stay inside the skill dir: relative, no
    parent-escapes, no null bytes."""
    if not rel or rel.startswith(("/", "\\")) or "\x00" in rel:
        return False
    parts = rel.replace("\\", "/").split("/")
    return all(p not in ("", ".", "..") for p in parts)


def _quarantine_authored_skill(workspace: Path, skill_dir: Path, rep) -> dict:
    """Relocate a just-authored skill whose bundled code scanned caution/dangerous
    into the import quarantine (the same surfaces review it: approve re-gates,
    reject deletes). Returns the tool-facing result."""
    import shutil

    qroot = workspace / ".durin" / "import-quarantine"
    dest = qroot / skill_dir.name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill_dir), str(dest))
    findings = [{"category": f.category, "severity": f.severity,
                 "where": f.where, "detail": f.detail} for f in rep.findings]
    (dest / ".scan.json").write_text(
        json.dumps({"source": "authored:agent", "verdict": rep.verdict,
                    "findings": findings}), encoding="utf-8")
    return {"quarantined": True, "name": skill_dir.name, "verdict": rep.verdict,
            "findings": findings,
            "note": "bundled code scanned as risky; the skill is NOT active — "
                    "it awaits review in the import quarantine"}


def dream_create_skill(workspace: Path, name: str, content: str,
                       rationale: str, attribution: "Attribution | None" = None,
                       files: dict[str, str] | None = None,
                       composition_judge=None,
                       composition_override: bool = False) -> dict:
    """Create a NEW skill authored by the dream: stamp mode=auto +
    provenance.source='dream', write SKILL.md (+ optional bundled files),
    commit. Refuses to overwrite an existing skill (that path is an edit,
    not a create).

    Bundled `files` (path → content, e.g. scripts) send the write through the
    same security scan imports get, BEFORE the skill activates: a `safe`
    verdict installs with the verdict stamped in provenance; `caution` or
    `dangerous` relocates the whole skill to the import quarantine for review
    instead of activating it.

    `composition_judge` (a prompt→text callable) enforces the composition
    doctrine at the boundary: a prose-only narration of a workflow-shaped
    procedure is rejected with the judge's reason so the author retries with
    feedback. `composition_override=True` skips the gate — the caller decides
    who may override (in-session that is the user's explicit word; the dream
    never overrides). Failure-open: no judge / judge error accepts."""
    if not _safe_name(name):
        return {"error": "invalid skill name"}
    if not rationale or not rationale.strip():
        return {"error": "rationale is required"}
    files = files or {}
    if not all(_safe_bundle_path(p) for p in files):
        return {"error": "invalid bundled file path (must be relative, inside the skill)"}
    md = _skill_md(workspace, name)
    if md.exists():
        return {"error": f"skill already exists: {name}"}
    if composition_judge is not None and not composition_override:
        from durin.agent.skills_doctrine import judge_composition
        ok, reason = judge_composition(content, workspace, composition_judge)
        if not ok:
            return {"error": f"composition gate: {reason}", "composition_rejected": True}
    store = _store_init(workspace)  # ensure git repo exists before mutating files
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(content, encoding="utf-8")
    if not (content.strip() and (_frontmatter_description(content) or _derive_description(content))):
        md.unlink()
        return {"error": "skill body has no derivable description"}
    for rel, body in files.items():
        target = md.parent / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(body), encoding="utf-8")
    _ensure_surface_frontmatter(md, name)

    scan_verdict = None
    if files:
        from durin.security.skill_scan import scan_skill
        rep = scan_skill(md.parent)
        if rep.verdict != "safe":
            return _quarantine_authored_skill(workspace, md.parent, rep)
        scan_verdict = rep.verdict

    def _stamp(data: dict) -> None:
        durin = ensure_durin(data)
        durin["mode"] = "auto"
        durin["provenance"] = {"source": "dream", "created_at": _today()}
        if scan_verdict is not None:
            durin["provenance"]["scan_verdict"] = scan_verdict

    _update_md(md, _stamp)
    sha = store.auto_commit(f"skill({name}): {rationale.strip()} [dream]",
                            trailers=attribution_to_trailers(attribution))
    _sync_index(workspace, name)
    return {"ok": True, "name": name, "commit": sha}


def dream_restructure_skill(workspace: Path, name: str, *, content: str,
                            rationale: str, files: dict[str, str] | None = None,
                            attribution: "Attribution | None" = None,
                            composition_judge=None,
                            composition_override: bool = False) -> dict:
    """Rewrite an EXISTING `auto` skill's SKILL.md body and (re)write bundled
    `files`, applying the SAME composition gate + security scan as
    `dream_create_skill`. This is the repair path the doctrine needs: it turns a
    prose-narrated procedure into a workflow-delegating body, or lifts an inlined
    deterministic snippet into a bundled script — mutations `apply_skill_edit`
    (bounded text replace, no new bundled files) and `dream_fuse_skills` cannot
    express. Refuses `manual` skills (the user owns those) and missing skills.

    On a `caution`/`dangerous` scan of the new bundled code the whole skill is
    relocated to the import quarantine (inactive, pending review) rather than
    activating risky code — the same posture as create."""
    if not _safe_name(name):
        return {"error": "invalid skill name"}
    if not rationale or not rationale.strip():
        return {"error": "rationale is required"}
    files = files or {}
    if not all(_safe_bundle_path(p) for p in files):
        return {"error": "invalid bundled file path (must be relative, inside the skill)"}
    loader = _loader(workspace)
    if loader.load_skill(name) is None:
        return {"error": f"skill not found: {name}"}
    if read_mode(workspace, name, loader) == "manual":
        return {"error": f"skill is manual, refusing: {name}"}
    if not (content.strip() and (_frontmatter_description(content) or _derive_description(content))):
        return {"error": "skill body has no derivable description"}
    if composition_judge is not None and not composition_override:
        from durin.agent.skills_doctrine import judge_composition
        ok, reason = judge_composition(content, workspace, composition_judge)
        if not ok:
            return {"error": f"composition gate: {reason}", "composition_rejected": True}
    store = _store_init(workspace)  # ensure git repo exists before mutating files
    dest = fork_on_write(workspace, name, loader)
    md = dest / "SKILL.md"
    md.write_text(content, encoding="utf-8")
    for rel, body in files.items():
        target = (dest / rel).resolve()
        if not target.is_relative_to(dest.resolve()):
            return {"error": "file escapes skill directory"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(body), encoding="utf-8")
    _ensure_surface_frontmatter(md, name)

    scan_verdict = None
    if files:
        from durin.security.skill_scan import scan_skill
        rep = scan_skill(dest)
        if rep.verdict != "safe":
            _unsync_index(workspace, name)
            return _quarantine_authored_skill(workspace, dest, rep)
        scan_verdict = rep.verdict

    if scan_verdict is not None:
        def _stamp(data: dict) -> None:
            durin = ensure_durin(data)
            prov = durin.get("provenance")
            if not isinstance(prov, dict):
                prov = {"source": "unknown", "created_at": _today()}
            prov["scan_verdict"] = scan_verdict
            durin["provenance"] = prov
        _update_md(md, _stamp)

    sha = store.auto_commit(f"skill({name}): {rationale.strip()} [dream]",
                            trailers=attribution_to_trailers(attribution))
    _sync_index(workspace, name)
    return {"ok": True, "name": name, "commit": sha}


def dream_fuse_skills(workspace: Path, *, target: str, content: str,
                      sources: list[str], rationale: str,
                      files: dict[str, str] | None = None,
                      composition_judge=None,
                      attribution: "Attribution | None" = None) -> dict:
    """Fuse `sources` into a new `target` skill. Refuses any `manual` source.
    Writes target (source=dream, mode=auto), removes workspace sources /
    disables builtin sources, one commit."""
    if not _safe_name(target) or not all(_safe_name(s) for s in sources):
        return {"error": "invalid skill name"}
    if not rationale.strip():
        return {"error": "rationale is required"}
    files = files or {}
    if not all(_safe_bundle_path(p) for p in files):
        return {"error": "invalid bundled file path (must be relative, inside the skill)"}
    bad = _skill_md_integrity(content)
    if bad is not None:
        return {"error": bad}
    for s in sources:
        if read_mode(workspace, s) == "manual":
            return {"error": f"source is manual, refusing: {s}"}
    if _skill_md(workspace, target).exists():
        return {"error": f"target already exists: {target}"}
    if composition_judge is not None:
        from durin.agent.skills_doctrine import judge_composition
        ok, reason = judge_composition(content, workspace, composition_judge)
        if not ok:
            return {"error": f"composition gate: {reason}", "composition_rejected": True}
    # Preserve source bundled files (scripts): gather them BEFORE the sources are
    # removed. Explicit `files` win on a path conflict; SKILL.md is never carried
    # (the merged body is authoritative). A fuse used to drop these silently.
    merged_files: dict[str, str] = {}
    for s in sources:
        sdir = _skills_dir(workspace) / s
        if not sdir.is_dir():
            continue
        for f in sorted(sdir.rglob("*")):
            if not f.is_file() or f.name == "SKILL.md":
                continue
            rel = f.relative_to(sdir).as_posix()
            if _safe_bundle_path(rel):
                merged_files.setdefault(rel, f.read_text(encoding="utf-8"))
    merged_files.update(files)

    store = _store_init(workspace)
    md = _skill_md(workspace, target)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(content, encoding="utf-8")
    for rel, body in merged_files.items():
        t = (md.parent / rel).resolve()
        if not t.is_relative_to(md.parent.resolve()):
            return {"error": "file escapes skill directory"}
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(str(body), encoding="utf-8")
    _ensure_surface_frontmatter(md, target)

    scan_verdict = None
    if merged_files:
        from durin.security.skill_scan import scan_skill
        rep = scan_skill(md.parent)
        if rep.verdict != "safe":
            # Risky merged code: quarantine the target, leave the sources intact
            # (the fuse is aborted for review rather than deleting working skills).
            return _quarantine_authored_skill(workspace, md.parent, rep)
        scan_verdict = rep.verdict

    def _stamp(data: dict) -> None:
        durin = ensure_durin(data)
        durin["mode"] = "auto"
        durin["provenance"] = {"source": "dream", "created_at": _today(),
                               "fused_from": list(sources)}
        if scan_verdict is not None:
            durin["provenance"]["scan_verdict"] = scan_verdict

    _update_md(md, _stamp)
    for s in sources:
        src_dir = _skills_dir(workspace) / s
        if src_dir.exists():
            shutil.rmtree(src_dir)
        else:  # builtin: workspace tombstone that disables model invocation
            tomb = _skills_dir(workspace) / s
            tomb.mkdir(parents=True, exist_ok=True)
            # disable_model_invocation lives at the TOP level of the
            # frontmatter (SkillsLoader reads it from get_skill_metadata,
            # not from metadata.durin); provenance stays under metadata.durin.
            (tomb / "SKILL.md").write_text(
                f"---\nname: {s}\ndisable_model_invocation: true\n"
                f"metadata:\n  durin:\n    mode: auto\n"
                f"    provenance:\n      source: dream\n      fused_into: {target}\n"
                f"---\nFused into `{target}`.\n", encoding="utf-8")
    sha = store.auto_commit(f"skill: fuse {sources} -> {target}: {rationale.strip()} [dream]",
                            trailers=attribution_to_trailers(attribution))
    # Multi-op index fan-out: the new target enters the index; every source
    # leaves it (workspace sources are rmtree'd; builtin sources become
    # disabled tombstones, which must not stay searchable).
    _sync_index(workspace, target)
    for s in sources:
        _unsync_index(workspace, s)
    return {"ok": True, "target": target, "removed": list(sources), "commit": sha}


def _parse_trailers(message: str) -> dict[str, str]:
    """Parse a trailing `Key: value` block (Actor/Session/Agent) from a commit message."""
    out: dict[str, str] = {}
    for line in reversed(message.splitlines()):
        s = line.strip()
        if not s:
            break  # blank line ends the trailer block (scanning bottom-up)
        if ": " in s:
            k, v = s.split(": ", 1)
            if k in ("Actor", "Session", "Agent"):
                out[k] = v.strip()
    return out


def _derive_actor(subject: str) -> str:
    """Best-effort actor for a trailer-less (legacy) commit, from the subject."""
    if "via web" in subject:
        return "user"
    if "[dream]" in subject or subject.startswith("skill: fuse"):
        return "curation"
    if "import from" in subject:
        return "import"
    if (": set mode=" in subject or ": curated @" in subject
            or subject.endswith(": remove") or subject.endswith(": revert to builtin")):
        return "system"
    return "agent"


def skill_history(workspace: Path, name: str) -> dict:
    """Per-skill history: {provenance, commits:[{sha,timestamp,subject,actor,session,agent}]}."""
    if _resolve_skill_dir(workspace, name) is None:
        return {"provenance": {}, "commits": []}
    text = read_skill_content(workspace, name) or ""
    prov = _durin_blob(text).get("provenance")
    commits: list[dict] = []
    for c in _store(workspace).log(max_entries=200, path=name):
        subject = c.message.splitlines()[0] if c.message else ""
        tr = _parse_trailers(c.message)
        commits.append({
            "sha": c.sha,
            "timestamp": c.timestamp,
            "subject": subject,
            "actor": tr.get("Actor") or _derive_actor(subject),
            "session": tr.get("Session"),
            "agent": tr.get("Agent"),
        })
    return {"provenance": prov if isinstance(prov, dict) else {}, "commits": commits}


_USER_EDIT_DIFF_CAP = 4000  # chars per commit diff shown to the curation judge


def user_edits_since_curation(workspace: Path, name: str) -> list[dict]:
    """User-authored commits (Actor: user) since the last curation stamp, each
    with its (path-scoped, bounded) unified diff.

    Dream's curation reads this straight from the skill git editorial so it can
    see WHAT the user changed by hand — not merely that a change happened — and
    treat it as intentional: evolve it only for a concrete reason, never revert
    it silently. Walks the skill's subtree log newest-first, stopping at the
    last `curated @` stamp."""
    if _resolve_skill_dir(workspace, name) is None:
        return []
    store = _store(workspace)
    out: list[dict] = []
    for c in store.log(max_entries=200, path=name):
        subject = c.message.splitlines()[0] if c.message else ""
        if ": curated @" in subject:
            break  # reached the last curation stamp — earlier edits already seen
        actor = _parse_trailers(c.message).get("Actor") or _derive_actor(subject)
        if actor != "user":
            continue
        diff = ""
        res = store.commit_diff(c.sha, path=name)
        if res is not None:
            _, patch = res
            diff = patch[:_USER_EDIT_DIFF_CAP]
            if len(patch) > _USER_EDIT_DIFF_CAP:
                diff += "\n… (diff truncated)"
        out.append({"sha": c.sha, "timestamp": c.timestamp,
                    "subject": subject, "diff": diff})
    return out


def web_list(workspace: Path) -> tuple[int, dict]:
    # Scan-augmented inventory (verdict + status) so the management panel can
    # surface security state. Imported locally to avoid a circular import
    # (skills_surface imports from skills_store). Scanning on a panel load is fine.
    from durin.agent.skills_surface import skills_inventory

    head = _store(workspace).log(max_entries=1)
    return 200, {
        "skills": skills_inventory(workspace),
        "store_head": ({"sha": head[0].sha, "at": head[0].timestamp} if head else None),
    }


def web_quarantine(workspace: Path) -> tuple[int, dict]:
    from durin.agent.skills_surface import quarantined_skills

    return 200, {"quarantined": quarantined_skills(workspace)}


def _import_allowlist() -> list[str]:
    from durin.config.loader import load_config
    try:
        return list(load_config().skills.security.allowlist)
    except Exception:  # noqa: BLE001
        return []


def _import_caps() -> tuple[int, int, int]:
    from durin.config.loader import load_config
    try:
        si = load_config().skills.security
        return (si.max_files, si.max_total_bytes, si.max_file_bytes)
    except Exception:  # noqa: BLE001
        return (100, 3 * 1024 * 1024, 1024 * 1024)


def _import_judge() -> tuple[str, str, str]:
    from durin.config.loader import load_config
    try:
        j = load_config().skills.security.llm_judge
        return (str(j.trigger or "off"), str(j.model or ""), str(j.max_severity or "caution"))
    except Exception:  # noqa: BLE001
        return ("off", "", "caution")


def web_import_resolve(workspace: Path, source: str) -> tuple[int, dict]:
    """`GET /api/skills/resolve?source=` — list the skill candidates a source
    points at (a repo may hold many). No download, no scan."""
    from durin.agent.skill_resolve import resolve_candidates

    res = resolve_candidates(source)
    return 200, {
        "candidates": [{"name": c.name, "ref": c.ref, "kind": c.kind, "detail": c.detail}
                       for c in res.candidates],
        "unresolved_reason": res.unresolved_reason,
    }


def _installed_skill_names(workspace: Path) -> set[str]:
    """Cheap set of available skill names (workspace + builtin) via the loader —
    no scanning. Used to flag search hits and short-circuit re-imports."""
    from durin.agent.skills_surface import _skill_dirs
    try:
        return set(_skill_dirs(Path(workspace)).keys())
    except Exception:  # noqa: BLE001
        return set()


def web_import_fetch(workspace: Path, source: str, replace: bool = False) -> tuple[int, dict]:
    """`POST /api/skills/import` — resolve a source, then:

    - multiple candidates → return the list to pick from;
    - already installed (and not ``replace``) → short-circuit BEFORE the
      expensive fetch+judge, returning ``already_installed`` so the UI can
      offer a re-install/override;
    - fetch into quarantine + scan, then gate via ``decide_action``:
        * ``allow`` (safe + allowlisted + no code) → auto-install and return
          ``installed`` — no manual second step;
        * ``confirm`` / ``block`` → leave in quarantine, return ``quarantined``
          + ``needs`` so the UI surfaces the decision.
    """
    from durin.agent.skill_resolve import resolve_candidates
    from durin.agent.skills_import import (
        SkillImportRefused,
        decide_action,
        fetch_candidate,
        install_imported_skill,
        validate_skill,
    )
    from durin.security.skill_scan import scan_skill

    res = resolve_candidates(source)
    if not res.candidates:
        return 200, {"unresolved_reason": res.unresolved_reason or "no skill found at source"}
    if len(res.candidates) > 1:
        return 200, {
            "candidates": [{"name": c.name, "ref": c.ref, "kind": c.kind, "detail": c.detail}
                           for c in res.candidates],
            "note": "multiple skills found; import one by passing its ref as source",
        }
    cand = res.candidates[0]
    if not replace and cand.name in _installed_skill_names(workspace):
        return 200, {"already_installed": cand.name, "source": cand.ref}

    qroot = Path(workspace) / ".durin" / "import-quarantine"
    mf, mt, mfb = _import_caps()
    jt, jm, jms = _import_judge()
    qdir = fetch_candidate(cand, quarantine_root=qroot,
                           max_files=mf, max_total_bytes=mt, max_file_bytes=mfb,
                           judge_trigger=jt, judge_model=jm, judge_max_severity=jms,
                           allowlist=_import_allowlist())
    rep = scan_skill(qdir)
    vr = validate_skill(qdir)
    findings = [{"category": f.category, "severity": f.severity,
                 "where": f.where, "detail": f.detail} for f in rep.findings]
    needs = decide_action(cand.ref, verdict=rep.verdict,
                          carries_code=vr.carries_code, allowlist=_import_allowlist())
    if needs == "allow":
        # The gate cleared it without friction — finish the install instead of
        # parking a trusted, safe skill in quarantine for a manual second step.
        try:
            r = install_imported_skill(
                workspace, qdir, source=cand.ref, allowlist=_import_allowlist(),
                confirmed=True, replace=replace)
            return 200, {"installed": r.get("name", cand.name), "source": cand.ref,
                         "verdict": rep.verdict, "needs": needs, "findings": findings,
                         "commit": r.get("commit")}
        except SkillImportRefused as exc:
            # Lost a race (skill appeared meanwhile) or a stricter re-scan —
            # fall back to the quarantine response carrying the new decision.
            return 200, {"quarantined": cand.name, "source": cand.ref,
                         "verdict": exc.verdict or rep.verdict, "needs": exc.action,
                         "findings": findings}
    return 200, {"quarantined": cand.name, "source": cand.ref,
                 "verdict": rep.verdict, "needs": needs, "findings": findings}


def web_skill_search(workspace: Path, query: str, limit: int = 0) -> tuple[int, dict]:
    """`GET /api/skills/search?query=&limit=` — search the configured registries.
    Search-only: returns ranked hits, each with a `ref` to import via the gate."""
    import asyncio

    from durin.agent.skill_registry import build_adapters, search_registries
    from durin.config.loader import load_config

    q = (query or "").strip()
    if not q:
        return 400, {"error": "query is required"}
    cfg = load_config()
    disc = cfg.skills.discovery
    hits = asyncio.run(search_registries(
        q,
        adapters=build_adapters(disc.registries),
        allowlist=list(cfg.skills.security.allowlist),
        limit=int(limit) or disc.search_limit,
    ))
    installed = _installed_skill_names(workspace)
    return 200, {"hits": [{"name": h.name, "ref": h.ref, "registry": h.registry,
                           "description": h.description, "signals": h.signals,
                           "installed": h.name in installed} for h in hits]}


def web_skill_describe(ref: str) -> tuple[int, dict]:
    """`GET /api/skills/describe?ref=` — read-only peek at a registry skill's
    SKILL.md frontmatter ``description`` (lazy-loaded by the search UI on expand).

    Resolves the ref the same way import does (``resolve_candidates`` — which uses
    the GitHub tree API to locate the actual SKILL.md, since a registry skillId is
    a NAME, not a path), then fetches just that SKILL.md and reads its frontmatter.
    Never executes or writes anything. Any failure degrades to an empty string."""
    ref = (ref or "").strip()
    if not ref:
        return 200, {"ref": ref, "description": "", "body": "",
                      "platforms": None, "requires": None}
    try:
        from durin.agent import skills_import as si
        from durin.agent.skill_resolve import resolve_candidates
        from durin.agent.skills_frontmatter import split_frontmatter

        cands = resolve_candidates(ref).candidates
        if not cands:
            return 200, {"ref": ref, "description": "", "body": "",
                          "platforms": None, "requires": None}
        cand = cands[0]
        if cand.kind == "https":
            url = cand.ref
        elif cand.kind == "github":
            owner, repo, branch, skill_dir = si._parse_github_ref(cand.ref)
            path = f"{skill_dir}/SKILL.md" if skill_dir else "SKILL.md"
            url = f"{si._GITHUB_RAW}/{owner}/{repo}/{branch}/{path}"
        elif cand.kind == "clawhub":
            slug = cand.ref[len("clawhub:"):] if cand.ref.startswith("clawhub:") else cand.name
            url = f"{si._CLAWHUB_API}/skills/{slug}/file?path=SKILL.md"
        else:
            return 200, {"ref": ref, "description": "", "body": "",
                          "platforms": None, "requires": None}
        raw = si._http_get_bytes(url)[:65_536]
        data, body = split_frontmatter(raw.decode("utf-8", errors="replace"))
        desc = str(data.get("description") or "").strip()
        plats = data.get("platforms")
        if isinstance(plats, str):
            plats = [plats]
        platforms = [str(p) for p in plats] if isinstance(plats, list) else None
        requires = None
        meta = data.get("metadata")
        if isinstance(meta, dict):
            durin = meta.get("durin")
            if isinstance(durin, dict) and isinstance(durin.get("requires"), dict):
                req = durin["requires"]
                requires = {
                    "bins": [str(b) for b in req.get("bins", [])] if isinstance(req.get("bins"), list) else [],
                    "env": [str(e) for e in req.get("env", [])] if isinstance(req.get("env"), list) else [],
                }
                if not requires["bins"] and not requires["env"]:
                    requires = None
        return 200, {"ref": ref, "description": desc[:1024], "body": body.strip(),
                      "platforms": platforms, "requires": requires}
    except Exception:  # noqa: BLE001 — describe is best-effort, never fatal
        return 200, {"ref": ref, "description": "", "body": "",
                      "platforms": None, "requires": None}


async def web_skill_install_deps(workspace: Path, name: str, *,
                                 bin_name: str | None = None,
                                 exec_run=None) -> tuple[int, dict]:
    """`GET /api/skills/{name}/install-deps` — install a skill's deps (or a
    specific bin's install spec) via the exec gate. Returns per-command results."""
    from durin.agent.skills_import import run_install_specs

    skill_dir = Path(workspace) / "skills" / name
    if not (skill_dir / "SKILL.md").is_file():
        return 404, {"error": f"skill not found: {name}"}
    if bin_name:
        specs = _spec_for_bin(skill_dir, bin_name)
    else:
        from durin.agent.skills_import import runnable_install_specs
        specs = runnable_install_specs(skill_dir)
    if not specs:
        return 200, {"ok": True, "results": [], "note": "no runnable install specs"}
    if exec_run is None:
        exec_run = _get_exec_run(workspace)
    results = await run_install_specs(specs, exec_run=exec_run)
    return 200, {"ok": True, "results": results}


def _get_exec_run(workspace: Path):
    """Create an async exec_run callable using the app config + ExecTool.

    ``ExecTool.create`` reads ``ctx.config.exec`` / ``.restrict_to_workspace`` /
    ``.process`` — all fields of ``ToolsConfig`` — so the ctx must carry the tools
    sub-config, NOT the top-level ``Config`` (which has no ``exec`` and raised
    AttributeError → HTTP 500 on any install_deps approve)."""
    from durin.agent.tools.shell import ExecTool
    from durin.config.loader import load_config

    tools_cfg = load_config().tools

    class _Ctx:
        def __init__(self, ws, config):
            self.workspace = ws
            self.config = config

    return ExecTool.create(_Ctx(workspace, tools_cfg)).execute


def _spec_for_bin(skill_dir: Path, bin_name: str) -> list[dict]:
    """Find the runnable install spec for a specific bin name."""
    from durin.security.requirements_scan import _PLATFORM_INSTALL_KINDS, _current_platform
    from durin.security.tool_catalog import load_catalog

    catalog = load_catalog(skill_dir.parent.parent)
    entry = catalog.get(bin_name)
    if not entry:
        return []
    platform = _current_platform()
    valid_kinds = _PLATFORM_INSTALL_KINDS.get(platform, ())
    primary = entry.get("primary", {})
    if primary.get("kind") in valid_kinds:
        return [{"kind": primary["kind"], "value": primary["value"],
                 "command": f"{primary['kind']} install {primary['value']}",
                 "needs_privileges": primary["kind"] == "apt"}]
    for alt in entry.get("alternatives", []):
        if alt.get("kind") in valid_kinds:
            return [{"kind": alt["kind"], "value": alt["value"],
                     "command": f"{alt['kind']} install {alt['value']}",
                     "needs_privileges": alt["kind"] == "apt"}]
    return []


async def web_skill_approve(workspace: Path, name: str, *, confirm: bool,
                            override: bool, replace: bool = False,
                            install_deps: bool = False,
                            exec_run=None) -> tuple[int, dict]:
    """`GET /api/skills/{name}/approve?...&install_deps=true` — install a
    quarantined skill through the import security gate, optionally auto-installing deps."""
    import json as _json

    from durin.agent.skills_import import SkillImportRefused, install_imported_skill

    qdir = Path(workspace) / ".durin" / "import-quarantine" / name
    if not (qdir / "SKILL.md").is_file():
        return 404, {"error": f"not in quarantine: {name}"}
    source = name
    sj = qdir / ".scan.json"
    if sj.is_file():
        try:
            source = _json.loads(sj.read_text()).get("source", name)
        except Exception:  # noqa: BLE001
            pass
    try:
        res = install_imported_skill(workspace, qdir, source=source,
                                     allowlist=_import_allowlist(),
                                     confirmed=confirm, override=override, replace=replace)
    except SkillImportRefused as exc:
        return 409, {"refused": exc.action, "verdict": exc.verdict, "message": str(exc)}

    if install_deps and exec_run:
        from durin.agent.skills_import import run_install_specs, runnable_install_specs
        skill_dir = Path(workspace) / "skills" / name
        specs = runnable_install_specs(skill_dir)
        if specs:
            res["deps_results"] = await run_install_specs(specs, exec_run=exec_run)
        else:
            res["deps_results"] = []
    return 200, res


def web_skill_reject(workspace: Path, name: str) -> tuple[int, dict]:
    """`GET /api/skills/{name}/reject` — discard a quarantined skill."""
    from durin.agent.skills_import import reject_quarantined

    res = reject_quarantined(workspace, name)
    return (400, res) if "error" in res else (200, res)


def web_skill_remove(workspace: Path, name: str) -> tuple[int, dict]:
    """`GET /api/skills/{name}/remove` — delete a workspace skill / revert a fork."""
    res = remove_skill(workspace, name)
    if "error" in res:
        status = 404 if "not found" in res["error"] else 400
        return status, res
    return 200, res


def _persist_judge_result(qdir, source: str, verdict: str, findings: list, summary: str) -> None:
    """Write the merged judge result to the quarantine ``.scan.json`` (shared by
    the HTTP and websocket audit paths). Preserves existing keys (e.g.
    ``requirements``) from a prior ``fetch_candidate`` scan."""
    import json as _json

    sj = qdir / ".scan.json"
    data: dict = {}
    if sj.is_file():
        try:
            loaded = _json.loads(sj.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:  # noqa: BLE001
            pass
    data.update({"source": source, "verdict": verdict, "findings": findings, "summary": summary})
    sj.write_text(_json.dumps(data), encoding="utf-8")


def web_skill_judge(workspace: Path, name: str) -> tuple[int, dict]:
    """`GET /api/skills/{name}/judge` — run the LLM judge ON-DEMAND over a
    quarantined skill, merge its findings into the quarantine .scan.json, and
    return the updated verdict + findings + summary. Errors carry a machine
    ``error_code`` (unreachable | parse | no_model) for a readable UI message."""
    import json as _json

    from durin.providers.base import LLMProvider
    from durin.security.skill_judge import JudgeError, judge_skill
    from durin.security.skill_scan import ScanReport, scan_skill

    qdir = Path(workspace) / ".durin" / "import-quarantine" / name
    if not (qdir / "SKILL.md").is_file():
        return 404, {"error": f"not in quarantine: {name}"}
    _, model, max_sev = _import_judge()
    det = scan_skill(qdir)
    try:
        from durin.memory.llm_invoke import judge_llm_invoke
        outcome = judge_skill(qdir, llm_invoke=judge_llm_invoke, model=model or "",
                              max_severity=max_sev)
    except JudgeError as exc:
        code = "parse" if "parse" in str(exc).lower() else "unreachable"
        return 200, {"name": name, "verdict": det.verdict, "judged": False,
                     "error": str(exc), "error_code": code}
    except Exception as exc:  # noqa: BLE001
        code = "unreachable" if LLMProvider._is_transient_error(str(exc)) else "no_model"
        return 200, {"name": name, "verdict": det.verdict, "judged": False,
                     "error": str(exc), "error_code": code}

    merged = ScanReport(findings=det.findings + outcome.findings)
    merged.tools = outcome.tools
    merged.judge_verdict = outcome.verdict
    findings = [{"category": f.category, "severity": f.severity, "where": f.where,
                 "detail": f.detail} for f in merged.findings]
    source = name
    sj = qdir / ".scan.json"
    if sj.is_file():
        try:
            source = _json.loads(sj.read_text()).get("source", name)
        except Exception:  # noqa: BLE001
            pass
    _persist_judge_result(qdir, source, merged.verdict, findings, outcome.summary)
    return 200, {"name": name, "verdict": merged.verdict, "findings": findings,
                 "summary": outcome.summary, "judged": True}


def _active_findings(rep) -> list[dict]:
    return [{"category": f.category, "severity": f.severity,
             "where": f.where, "detail": f.detail} for f in rep.findings]


def web_skill_review_user(workspace: Path, name: str, note: str = "") -> tuple[int, dict]:
    """`POST /api/v1/skills/{name}/review` — user marks an ACTIVE skill reviewed
    (override to safe). Persists a review keyed by content hash + findings."""
    from durin.agent.skills_surface import _skill_dirs
    from durin.security.skill_reviews import record_review
    from durin.security.skill_scan import scan_skill

    d = _skill_dirs(Path(workspace)).get(name)
    if d is None or not (d / "SKILL.md").is_file():
        return 404, {"error": f"skill not found: {name}"}
    rep = scan_skill(d)
    findings = _active_findings(rep)
    review = record_review(Path(workspace), name, d, by="user", verdict="safe",
                           original=rep.verdict, findings=findings, note=note)
    return 200, {"name": name, "reviewed": True, "review": review,
                 "verdict": rep.verdict, "findings": findings}


def web_skill_unreview(workspace: Path, name: str) -> tuple[int, dict]:
    """`DELETE /api/v1/skills/{name}/review` — reopen (drop) a skill's review."""
    from durin.security.skill_reviews import clear_review
    cleared = clear_review(Path(workspace), name)
    return 200, {"name": name, "reviewed": False, "cleared": cleared}


def record_review_from_judge(workspace: Path, name: str, skill_dir, *, judge_verdict,
                             merged_findings, summary, original) -> dict | None:
    """Persist an LLM review for an ACTIVE skill — only when the judge did NOT
    confirm dangerous (i.e. it cleared to safe/caution). Returns the review or
    None when nothing was recorded."""
    from durin.security.skill_reviews import record_review
    if judge_verdict not in ("safe", "caution"):
        return None
    return record_review(Path(workspace), name, skill_dir, by="llm",
                         verdict=judge_verdict, original=original,
                         findings=merged_findings, note=summary or "")


def web_github_token_test(secret_name: str) -> tuple[int, dict]:
    """`GET /api/skills/github-token-test?secret=` — verify a GitHub-token secret
    against the GitHub API (rate_limit). Returns {ok, remaining, limit} or {ok:false, error}."""
    import asyncio
    import threading

    from durin.security.network import ssrf_safe_async_client
    from durin.security.secrets import resolve_secret

    name = (secret_name or "").strip()
    if not name:
        return 400, {"error": "secret name required"}
    try:
        token = str(resolve_secret(f"${{secret:{name}}}") or "")
    except Exception:  # noqa: BLE001
        return 200, {"ok": False, "error": f"secret not found: {name}"}
    if not token:
        return 200, {"ok": False, "error": "secret resolved empty"}

    box: dict = {}

    async def _go() -> tuple[int, dict]:
        async with ssrf_safe_async_client() as client:
            r = await client.get(
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
                timeout=10.0)
            ctype = r.headers.get("content-type", "")
            return r.status_code, (r.json() if "json" in ctype else {})

    def _run() -> None:
        try:
            box["v"] = asyncio.run(_go())
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    if "e" in box:
        return 200, {"ok": False, "error": str(box["e"])}
    status, data = box["v"]
    if status == 200:
        core = data.get("resources", {}).get("core", {}) if isinstance(data, dict) else {}
        return 200, {"ok": True, "remaining": core.get("remaining"), "limit": core.get("limit")}
    if status == 401:
        return 200, {"ok": False, "error": "GitHub rejected the token (401)"}
    return 200, {"ok": False, "error": f"GitHub returned {status}"}


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


def web_files(workspace: Path, name: str) -> tuple[int, dict]:
    if _resolve_skill_dir(workspace, name) is None:
        return 404, {"error": f"skill not found: {name}"}
    return 200, {"files": skill_files(workspace, name)}


def web_file_get(workspace: Path, name: str, path: str) -> tuple[int, dict]:
    res = read_skill_file(workspace, name, path)
    if res is None:
        return 404, {"error": "file not found"}
    return 200, res


def web_file_save(workspace: Path, name: str, path: str, content: str, *,
                  attribution: "Attribution | None" = None) -> tuple[int, dict]:
    res = save_skill_file(workspace, name, path, content,
                          rationale=f"edited {path} via web", attribution=attribution)
    return (200, res) if res.get("ok") else (400, res)


def web_history(workspace: Path, name: str) -> tuple[int, dict]:
    if _resolve_skill_dir(workspace, name) is None:
        return 404, {"error": f"skill not found: {name}"}
    return 200, skill_history(workspace, name)


def web_commit_diff(workspace: Path, name: str, sha: str) -> tuple[int, dict]:
    """Unified diff of one commit, scoped to skill ``name``'s subtree."""
    if not _safe_name(name):
        return 404, {"error": "invalid skill name"}
    store = _store(workspace)
    res = store.commit_diff(sha, path=name)
    if res is None:
        return 404, {"error": "commit not found"}
    info, patch = res
    return 200, {"sha": info.sha, "patch": patch}
