"""Upstream drift detection for imported skills (§8.D). Re-fetches a skill's
recorded origin, scans the new content (§8.C), and reports whether it drifted +
whether dream may auto-incorporate it (decide_action 'allow') or it needs the
human gate. It NEVER touches the installed skill — drift is a SIGNAL for the
dream curation pass, which EVOLVES (incorporates) rather than replaces, so a
locally-evolved skill is never overwritten."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

_REAL_REPO_PREFIXES = ("github:", "https://", "http://", "clawhub:")


@dataclass
class DriftReport:
    name: str
    source: str
    action: str        # 'allow' (dream may incorporate) | 'confirm' | 'block' (human gate)
    verdict: str       # §8.C verdict of the NEW upstream content
    carries_code: bool
    qdir: Path         # the fetched upstream, in a drift quarantine
    upstream_md: str   # the new SKILL.md content (context for the curation judge)


def check_upstream_drift(workspace, name, *, allowlist=None) -> DriftReport | None:
    from durin.agent import skills_store as ss
    from durin.agent.skill_resolve import resolve_candidates
    from durin.agent.skills_import import (
        _content_hash,
        decide_action,
        fetch_candidate,
        validate_skill,
    )
    from durin.security.skill_scan import scan_skill

    allow = list(allowlist or [])
    text = ss.read_skill_content(workspace, name)
    if text is None:
        return None
    prov = ss._durin_blob(text).get("provenance")
    if not isinstance(prov, dict):
        return None
    source = str(prov.get("source") or "")
    if not source.startswith(_REAL_REPO_PREFIXES):
        return None  # local / dream / builtin / no source → not upstream-drift-checkable
    stored_hash = str(prov.get("content_hash") or "")

    res = resolve_candidates(source)
    if not res.candidates:
        return None  # upstream unreachable / gone
    cand = next((c for c in res.candidates if c.name == name), res.candidates[0])

    drift_root = Path(workspace) / ".durin" / "drift-quarantine"
    qdir = fetch_candidate(cand, quarantine_root=drift_root, allowlist=allow)
    if _content_hash(qdir) == stored_hash:
        shutil.rmtree(qdir, ignore_errors=True)
        return None  # no drift

    vr = validate_skill(qdir)
    rep = scan_skill(qdir)
    action = decide_action(source, verdict=rep.verdict,
                           carries_code=vr.carries_code, allowlist=allow)
    return DriftReport(
        name=name, source=source, action=action, verdict=rep.verdict,
        carries_code=vr.carries_code, qdir=qdir,
        upstream_md=(qdir / "SKILL.md").read_text(encoding="utf-8"),
    )
