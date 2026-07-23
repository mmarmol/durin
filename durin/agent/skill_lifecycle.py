"""Unverified-origin sweep (Part C). A workspace skill in `skills/` WITHOUT
`metadata.durin.provenance` reached the filesystem outside every durin path (a
registry CLI, a manual copy) — never scanned or gated. Relocate it to the import
quarantine, scan it through the import security gate, and prepend an `unverified_origin` finding. The agent
loads only from `skills/` (+ builtins), so relocating makes it INERT automatically
— no retrieval filter needed. Surfaced in the existing quarantine; approve
re-gates, reject deletes. Idempotent."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from durin.agent.skills_frontmatter import frontmatter_broken
from durin.agent.skills_store import _bundled_file_count, _durin_blob
from durin.security.skill_scan import scan_skill

_UNVERIFIED_DETAIL = (
    "In skills/ without durin provenance — it entered outside the security gate "
    "(a registry CLI or a manual copy). Its instructions and any bundled code were "
    "never scanned or approved; it could exfiltrate data or manipulate the agent. "
    "Audit and approve to use it, or reject it."
)


def _attributed_source(workspace: Path, skill_name: str) -> str:
    """Best-effort attribution for a skill swept into quarantine.

    Returns `agent:session:<id>` when the most recent skills-store commit that
    touched this skill's own path (`GitStore.log(path=skill_name)`, the same
    path-scoping `skill_history`/`user_edits_since_curation` use) carries a
    `Session:` trailer, else the `unverified:workspace` fallback. Path-scoping
    — not a text search — is what keeps a generically-named skill from being
    attributed to an unrelated commit that merely mentions its name. Never
    raises — an uninitialized store or any git-log failure degrades straight
    to the fallback."""
    try:
        from durin.agent.skills_store import _store
        entries = _store(workspace).log(max_entries=50, path=skill_name)
    except Exception:
        return "unverified:workspace"
    for entry in entries:
        msg = getattr(entry, "message", "") or ""
        for line in msg.splitlines():
            if line.strip().lower().startswith("session:"):
                sid = line.split(":", 1)[1].strip()
                if sid:
                    return f"agent:session:{sid}"
    return "unverified:workspace"


_BROKEN_FM_ISSUE = (
    "SKILL.md frontmatter is not valid YAML — hand-written fields like the "
    "description likely contain an unquoted colon, so name/description (and "
    "possibly provenance) cannot be read."
)


def _observe_broken_frontmatter(workspace: Path, name: str) -> None:
    """Log one OPEN observation about the unparseable frontmatter. The sweep
    runs on every skills listing, so skip when the same observation is already
    open — never bump/commit per sweep. Never raises."""
    try:
        from durin.agent.skill_observations import log_observation, open_observations
        if any(r.get("issue") == _BROKEN_FM_ISSUE
               for r in open_observations(workspace, name)):
            return
        log_observation(
            workspace, skill=name, kind="correction", issue=_BROKEN_FM_ISSUE,
            improvement="Repair the frontmatter YAML (quote scalars containing "
                        "':') so the skill's name, description, mode and "
                        "provenance parse again.")
    except Exception:  # noqa: BLE001 — observability must not break the sweep
        pass


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
        if frontmatter_broken(text):
            # The YAML is unparseable, so provenance may exist but be hidden
            # (_durin_blob already tries a metadata-only recovery). Surface the
            # breakage as an observation either way — the file needs repair.
            _observe_broken_frontmatter(workspace, d.name)
        prov = _durin_blob(text).get("provenance")
        if isinstance(prov, dict) and prov:
            continue  # has provenance → gated import / dream / forked builtin → keep
        dest = qroot / d.name
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(dest))
        source = _attributed_source(workspace, d.name)
        # Count bundled files BEFORE .scan.json is written below — it would
        # otherwise count the scan artifact itself as a "bundled file".
        files_count = _bundled_file_count(dest)
        rep = scan_skill(dest)
        findings = [{"category": "unverified_origin", "severity": "caution",
                     "where": "SKILL.md", "detail": _UNVERIFIED_DETAIL}]
        findings += [{"category": f.category, "severity": f.severity,
                      "where": f.where, "detail": f.detail} for f in rep.findings]
        verdict = "dangerous" if rep.verdict == "dangerous" else "caution"
        (dest / ".scan.json").write_text(
            json.dumps({"source": source, "verdict": verdict,
                        "findings": findings}), encoding="utf-8")
        if source.startswith("agent:session:"):
            from durin.agent.tools._telemetry import emit_tool_event
            emit_tool_event("skill.authored", {
                "name": d.name, "actor": "agent", "session": source.split(":", 2)[2],
                "model": None, "ramp": "backstop", "composition": "compliant",
                "scan_verdict": verdict, "files_count": files_count})
        moved.append(d.name)
    return moved
