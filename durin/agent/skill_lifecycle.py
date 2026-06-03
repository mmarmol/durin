"""Unverified-origin sweep (Part C). A workspace skill in `skills/` WITHOUT
`metadata.durin.provenance` reached the filesystem outside every durin path (a
registry CLI, a manual copy) — never scanned or gated. Relocate it to the import
quarantine, scan it (§8.C), and prepend an `unverified_origin` finding. The agent
loads only from `skills/` (+ builtins), so relocating makes it INERT automatically
— no retrieval filter needed. Surfaced in the existing quarantine; approve
re-gates, reject deletes. Idempotent."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from durin.agent.skills_store import _durin_blob
from durin.security.skill_scan import scan_skill

_UNVERIFIED_DETAIL = (
    "In skills/ without durin provenance — it entered outside the security gate "
    "(a registry CLI or a manual copy). Its instructions and any bundled code were "
    "never scanned or approved; it could exfiltrate data or manipulate the agent. "
    "Audit and approve to use it, or reject it."
)


def sweep_unverified_skills(workspace) -> list[str]:
    """Relocate no-provenance workspace skills to the import quarantine. Returns
    the names relocated. Idempotent (a second call finds nothing)."""
    workspace = Path(workspace)
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return []
    qroot = workspace / ".durin" / "import-quarantine"
    moved: list[str] = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir() or not (d / "SKILL.md").is_file():
            continue
        try:
            text = (d / "SKILL.md").read_text(encoding="utf-8")
        except OSError:
            continue
        prov = _durin_blob(text).get("provenance")
        if isinstance(prov, dict) and prov:
            continue  # has provenance → gated import / dream / forked builtin → keep
        dest = qroot / d.name
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(dest))
        rep = scan_skill(dest)
        findings = [{"category": "unverified_origin", "severity": "caution",
                     "where": "SKILL.md", "detail": _UNVERIFIED_DETAIL}]
        findings += [{"category": f.category, "severity": f.severity,
                      "where": f.where, "detail": f.detail} for f in rep.findings]
        verdict = "dangerous" if rep.verdict == "dangerous" else "caution"
        (dest / ".scan.json").write_text(
            json.dumps({"source": "unverified:workspace", "verdict": verdict,
                        "findings": findings}), encoding="utf-8")
        moved.append(d.name)
    return moved
