"""Skill import (§6.B) + validation. Deterministic, dependency-free where it
matters. The security SCAN lives in durin/security/skill_scan.py."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from durin.agent.skills_frontmatter import split_frontmatter

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass
class ValidationReport:
    name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    carries_code: bool = False
    code_artifacts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_skill(skill_dir: Path) -> ValidationReport:
    """Validate a skill dir against agentskills.io + detect code. name/description
    missing are ERRORS; name-shape issues are WARNINGS (import-friendly)."""
    skill_dir = Path(skill_dir)
    md = skill_dir / "SKILL.md"
    rep = ValidationReport(name=skill_dir.name)
    if not md.is_file():
        rep.errors.append("no SKILL.md")
        return rep
    data, _ = split_frontmatter(md.read_text(encoding="utf-8"))
    name = str(data.get("name") or "").strip()
    desc = str(data.get("description") or "").strip()
    if name:
        rep.name = name
        if not _NAME_RE.match(name):
            rep.warnings.append(f"name {name!r} not agentskills.io-conformant (1-64 lowercase/digits/hyphens)")
        if name != skill_dir.name:
            rep.warnings.append(f"name {name!r} != directory {skill_dir.name!r}")
    else:
        rep.errors.append("missing required 'name'")
    if not desc:
        rep.errors.append("missing required 'description'")
    elif len(desc) > 1024:
        rep.warnings.append("description exceeds 1024 chars")
    scripts = skill_dir / "scripts"
    if scripts.is_dir():
        for p in sorted(scripts.rglob("*")):
            if p.is_file():
                rep.code_artifacts.append(str(p.relative_to(skill_dir)))
    meta = data.get("metadata")
    if isinstance(meta, dict):
        for vendor, blob in meta.items():
            if isinstance(blob, dict) and blob.get("install"):
                rep.code_artifacts.append(f"metadata.{vendor}.install")
    rep.carries_code = bool(rep.code_artifacts)
    return rep


def decide_action(source: str, *, verdict: str, carries_code: bool, allowlist: list[str]) -> str:
    """§8.C trust×verdict gate. Returns 'allow' | 'confirm' | 'block'.
    'block' needs an explicit override; 'confirm' needs confirmation. The
    dangerous-block and carries-code-confirm have no opt-out; only the source
    check is loosened by the allowlist."""
    if verdict == "dangerous":
        return "block"
    allowlisted = any(source.startswith(p) for p in allowlist if p)
    if carries_code or verdict == "caution" or not allowlisted:
        return "confirm"
    return "allow"
