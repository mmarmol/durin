"""SkillPage — a parsed view of skills/<name>/SKILL.md for indexing.

Mirrors entity_page.EntityPage but rooted at workspace/skills/ and authored by
the git-backed skills_store. `from_file` returns None for missing/unreadable
files so rebuild walkers skip silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillPage:
    name: str
    description: str
    body: str
    mode: str
    disabled: bool
    path: Path

    @classmethod
    def from_file(cls, path: Path) -> "SkillPage | None":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        from durin.agent.skills_frontmatter import split_frontmatter

        data, body = split_frontmatter(text)
        meta = data.get("metadata")
        durin = meta.get("durin") if isinstance(meta, dict) else None
        durin = durin if isinstance(durin, dict) else {}
        disabled = bool(
            durin.get("disable_model_invocation")
            or data.get("disable_model_invocation")
        )
        return cls(
            name=str(data.get("name") or path.parent.name),
            description=str(data.get("description", "")),
            body=body,
            mode=str(durin.get("mode", "")),
            disabled=disabled,
            path=path,
        )
