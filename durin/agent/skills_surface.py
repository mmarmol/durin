"""Skills management surface — the shared read model (inventory + quarantine)
for the CLI, web panel, and chat. Augments the fast list_skills_info with the
§8.C security verdict; kept separate so the agent context path stays scan-free."""
from __future__ import annotations

import json
from pathlib import Path

from durin.agent.skills_store import _loader, list_skills_info
from durin.security.skill_scan import scan_skill


def _scan_payload(skill_dir: Path) -> dict:
    rep = scan_skill(skill_dir)
    return {"verdict": rep.verdict,
            "findings": [{"category": f.category, "severity": f.severity,
                          "where": f.where, "detail": f.detail} for f in rep.findings]}


def _skill_dirs(workspace: Path) -> dict[str, Path]:
    """Map skill name -> the real dir holding its SKILL.md (workspace or builtin).

    Resolves via the same loader skills_store uses (its patchable builtin
    global, single source of truth) so an UNFORKED builtin is scanned at its
    real builtin path, not skipped. ``entry['path']`` is the resolved SKILL.md
    (workspace shadows builtin); its parent is the skill dir."""
    loader = _loader(Path(workspace))
    return {e["name"]: Path(e["path"]).parent
            for e in loader.list_skills(filter_unavailable=False)}


def skills_inventory(workspace) -> list[dict]:
    """Active skills (E1 fields) + §8.C verdict/findings + status='active'."""
    workspace = Path(workspace)
    dirs = _skill_dirs(workspace)
    out = []
    for info in list_skills_info(workspace):
        entry = dict(info)
        entry["status"] = "active"
        d = dirs.get(info["name"])
        if d is not None and d.is_dir():
            entry.update(_scan_payload(d))
        else:
            entry.update({"verdict": "safe", "findings": []})
        out.append(entry)
    return out


def quarantined_skills(workspace) -> list[dict]:
    """Skills awaiting import decision in .durin/import-quarantine/ (filled by §6.B)."""
    workspace = Path(workspace)
    qroot = workspace / ".durin" / "import-quarantine"
    out = []
    if not qroot.is_dir():
        return out
    for d in sorted(qroot.iterdir()):
        if not (d / "SKILL.md").is_file():
            continue
        entry = {"name": d.name, "status": "quarantined", "source": "", "verdict": "", "findings": []}
        sj = d / ".scan.json"
        if sj.is_file():
            try:
                meta = json.loads(sj.read_text())
                entry["source"] = meta.get("source", "")
                entry["verdict"] = meta.get("verdict", "")
                entry["findings"] = meta.get("findings", [])
            except Exception:
                pass
        out.append(entry)
    return out
