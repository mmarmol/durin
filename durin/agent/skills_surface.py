"""Skills management surface — the shared read model (inventory + quarantine)
for the CLI, web panel, and chat. Augments the fast list_skills_info with the
security verdict; kept separate so the agent context path stays scan-free."""
from __future__ import annotations

import json
from pathlib import Path

from durin.agent.skills_store import _loader, list_skills_info, removable_action
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
    """Active skills (E1 fields) + security verdict/findings + status='active'."""
    workspace = Path(workspace)
    from durin.agent.skill_lifecycle import sweep_unverified_skills
    sweep_unverified_skills(workspace)
    loader = _loader(workspace)
    dirs = _skill_dirs(workspace)
    from durin.agent.skills_frontmatter import split_frontmatter
    from durin.security.requirements_scan import resolve_display
    from durin.security.tool_catalog import load_catalog
    catalog = load_catalog(workspace)
    out = []
    for info in list_skills_info(workspace):
        entry = dict(info)
        entry["status"] = "active"
        entry["removable"] = removable_action(workspace, info["name"], loader)
        d = dirs.get(info["name"])
        if d is not None and d.is_dir():
            entry.update(_scan_payload(d))
            prov_verdict = (info.get("provenance") or {}).get("verdict")
            if prov_verdict:
                entry["verdict"] = prov_verdict
        else:
            entry.update({"verdict": "safe", "findings": []})

        # A user/LLM "Revisada" override (workspace-level, hash + findings
        # pinned). Surfaced as a separate block — verdict/findings are preserved
        # so the report still shows the underlying deterministic findings.
        if d is not None and d.is_dir():
            from durin.security.skill_reviews import get_review
            review = get_review(workspace, info["name"], d, entry.get("findings") or [])
            if review:
                entry["review"] = {k: review.get(k)
                                   for k in ("by", "verdict", "original", "note", "at")}

        req_manifest = None
        md = d / "SKILL.md" if d else None
        if md and md.is_file():
            try:
                fdata, _ = split_frontmatter(md.read_text(encoding="utf-8"))
                durin = (fdata.get("metadata") or {}).get("durin", {})
                if isinstance(durin, dict) and isinstance(durin.get("requirements"), dict):
                    req_manifest = durin["requirements"]
            except Exception:  # noqa: BLE001
                pass
        if req_manifest:
            entry["requirements"] = resolve_display(
                req_manifest, catalog=catalog)
        elif d is not None and d.is_dir():
            from durin.security.requirements_scan import extract_requirements
            req_manifest = extract_requirements(d, workspace=workspace)
            entry["requirements"] = resolve_display(req_manifest, catalog=catalog)
        else:
            entry["requirements"] = None
        out.append(entry)
    return out


def quarantined_skills(workspace) -> list[dict]:
    """Skills awaiting import decision in .durin/import-quarantine/."""
    workspace = Path(workspace)
    from durin.agent.skill_lifecycle import sweep_unverified_skills
    sweep_unverified_skills(workspace)
    qroot = workspace / ".durin" / "import-quarantine"
    out = []
    if not qroot.is_dir():
        return out
    from durin.security.requirements_scan import resolve_display
    from durin.security.tool_catalog import load_catalog
    catalog = load_catalog(workspace)
    for d in sorted(qroot.iterdir()):
        if not (d / "SKILL.md").is_file():
            continue
        entry = {"name": d.name, "status": "quarantined", "source": "",
                 "verdict": "", "findings": [], "trust_prefix": "", "install_specs": [],
                 "needs": "confirm", "reasons": []}
        sj = d / ".scan.json"
        meta = None
        if sj.is_file():
            try:
                meta = json.loads(sj.read_text())
                entry["source"] = meta.get("source", "")
                entry["verdict"] = meta.get("verdict", "")
                entry["findings"] = meta.get("findings", [])
            except Exception:
                pass
        raw_req = meta.get("requirements") if isinstance(meta, dict) else None
        if isinstance(raw_req, dict):
            entry["requirements"] = resolve_display(
                raw_req, catalog=catalog)
        else:
            entry["requirements"] = None
        from durin.agent.skills_import import declared_install_specs, trust_prefix_for
        entry["install_specs"] = declared_install_specs(d)
        if entry["source"]:
            entry["trust_prefix"] = trust_prefix_for(entry["source"])

        # The gate decision (decide_action) plus its plain-language drivers, so
        # the UI can explain *why* approval is required — even when the skill is
        # not insecure (e.g. the source simply isn't in the trust allowlist).
        from durin.agent.skills_import import decide_action, validate_skill
        from durin.agent.skills_store import _import_allowlist

        allowlist = _import_allowlist()
        vr = validate_skill(d)
        verdict = entry["verdict"]
        entry["needs"] = decide_action(entry["source"], verdict=verdict,
                                       carries_code=vr.carries_code, allowlist=allowlist)
        reasons: list[dict] = []
        if verdict == "dangerous":
            reasons.append({"code": "verdict_dangerous", "detail": ""})
        elif verdict == "caution":
            reasons.append({"code": "verdict_caution", "detail": ""})
        if vr.carries_code:
            reasons.append({"code": "carries_code",
                            "detail": ", ".join(vr.code_artifacts[:8])})
        if entry["source"] and not any(entry["source"].startswith(p) for p in allowlist if p):
            reasons.append({"code": "untrusted_source", "detail": entry["source"]})
        if entry["install_specs"]:
            reasons.append({"code": "declared_deps",
                            "detail": ", ".join(entry["install_specs"])})
        entry["reasons"] = reasons
        out.append(entry)
    return out
