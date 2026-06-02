"""Service layer for durin's skill versioning + mode system.

All skill mutations go through here so the tool, the /skills command, and the
web routes share one implementation (and one git store). Pure functions over a
workspace Path — directly unit-testable with tmp_path.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
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


def _body_hash(text: str) -> str:
    _data, body = split_frontmatter(text)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def needs_curation(workspace: Path, name: str) -> bool:
    """True when the skill is new or its BODY changed since last curated."""
    text = read_skill_content(workspace, name)
    if text is None:
        return False
    prov = _durin_blob(text).get("provenance")
    stored = prov.get("dream_processed_through") if isinstance(prov, dict) else None
    return stored != _body_hash(text)


def mark_curated(workspace: Path, name: str) -> str | None:
    """Stamp provenance.dream_processed_through = current body hash + commit."""
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

    _update_md(dest / "SKILL.md", _set)
    return store.auto_commit(f"skill({name}): curated @ {h}")


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
    return store.auto_commit(f"skill({name}): set mode={mode}")


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
            return {"error": "old text not unique"}
        updated = content.replace(old, new, 1)

    if mode == "manual" and not confirm:
        return {
            "proposed": True, "mode": "manual", "name": name, "file": file,
            "note": "skill is manual; re-call with confirm=true after the user approves",
            "preview": _preview(content, updated),
        }
    target.write_text(updated, encoding="utf-8")
    sha = store.auto_commit(f"skill({name}): {rationale.strip()}")
    return {"ok": True, "name": name, "file": file, "mode": mode, "commit": sha}


def save_skill_content(workspace: Path, name: str, content: str,
                       rationale: str = "edit via web") -> dict:
    """Full-content overwrite of a MANUAL skill's SKILL.md (web edit surface)."""
    if not _safe_name(name):
        return {"error": "invalid skill name"}
    if read_mode(workspace, name) != "manual":
        return {"error": "skill is not manual; flip it to manual to edit"}
    store = _store_init(workspace)  # ensure git repo exists before mutating files
    dest = fork_on_write(workspace, name)
    (dest / "SKILL.md").write_text(content, encoding="utf-8")
    sha = store.auto_commit(f"skill({name}): {rationale}")
    return {"ok": True, "name": name, "commit": sha}


def dream_create_skill(workspace: Path, name: str, content: str,
                       rationale: str) -> dict:
    """Create a NEW skill authored by the dream: stamp mode=auto +
    provenance.source='dream', write SKILL.md, commit. Refuses to overwrite
    an existing skill (that path is an edit, not a create)."""
    if not _safe_name(name):
        return {"error": "invalid skill name"}
    if not rationale or not rationale.strip():
        return {"error": "rationale is required"}
    md = _skill_md(workspace, name)
    if md.exists():
        return {"error": f"skill already exists: {name}"}
    store = _store_init(workspace)  # ensure git repo exists before mutating files
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(content, encoding="utf-8")

    def _stamp(data: dict) -> None:
        durin = ensure_durin(data)
        durin["mode"] = "auto"
        durin["provenance"] = {"source": "dream", "created_at": _today()}

    _update_md(md, _stamp)
    sha = store.auto_commit(f"skill({name}): {rationale.strip()} [dream]")
    return {"ok": True, "name": name, "commit": sha}


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
