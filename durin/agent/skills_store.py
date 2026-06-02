"""Service layer for durin's skill versioning + mode system.

All skill mutations go through here so the tool, the /skills command, and the
web routes share one implementation (and one git store). Pure functions over a
workspace Path — directly unit-testable with tmp_path.
"""
from __future__ import annotations

import datetime as _dt
import difflib  # noqa: F401 — used in Task 4 (apply_skill_edit)
import shutil
from pathlib import Path

from durin.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader
from durin.agent.skills_frontmatter import ensure_durin, join_frontmatter, split_frontmatter
from durin.utils.gitstore import (
    GitStore,  # noqa: F401 — used in Task 4 (_store / save_skill_content)
)


def _skills_dir(workspace: Path) -> Path:
    return Path(workspace) / "skills"


def _skill_md(workspace: Path, name: str) -> Path:
    return _skills_dir(workspace) / name / "SKILL.md"


def _store(workspace: Path) -> GitStore:  # noqa: F401 — used in Task 4
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
