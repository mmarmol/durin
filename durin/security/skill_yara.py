"""Optional YARA signature scan (spec §4.f).

Guarded: a no-op returning ``[]`` when the ``[skill-yara]`` extra (yara-python)
is absent or no rules are present. Never raises out of a scan. Rules live in
``yara_rules/`` and are refreshed from a maintained feed (see yara_updater); durin
consumes signatures, it does not author them.
"""
from __future__ import annotations

import logging
from pathlib import Path

from durin.security.skill_scan import Finding

logger = logging.getLogger(__name__)

try:
    import yara as _yara
except Exception:  # noqa: BLE001 — optional extra; absent -> guarded no-op
    _yara = None

# Cap bytes read per file: signatures match headers/markers, not whole large blobs.
_MAX_BYTES = 1_048_576


def yara_available() -> bool:
    """True when the yara-python engine is importable."""
    return _yara is not None


def _rules_dir() -> Path:
    return Path(__file__).parent / "yara_rules"


def _compiled():
    """Compile the active rule set, or None when absent / non-compiling.

    Opportunistically refreshes from the configured feed when stale (best-effort;
    a failed refresh keeps the existing rules). No feed configured -> no refresh.
    """
    rd = _rules_dir()
    try:
        from durin.config.loader import load_config
        from durin.security.yara_updater import is_stale, refresh_rules
        ycfg = load_config().skills.security.yara
        if ycfg.feed_url and is_stale(rd, ycfg.refresh_hours):
            refresh_rules(rd, ycfg.feed_url, ycfg.feed_pin, sha256=None)
    except Exception as exc:  # noqa: BLE001 — refresh is best-effort
        logger.debug("YARA refresh skipped: %s", exc)
    files = {p.name: str(p) for p in rd.glob("*.yar")} if rd.is_dir() else {}
    if not files:
        return None
    try:
        return _yara.compile(filepaths=files)
    except Exception as exc:  # noqa: BLE001 — a bad rule set must not break scanning
        logger.warning("YARA rule compile failed (skipping): %s", exc)
        return None


def scan_yara(skill_dir: Path) -> list[Finding]:
    """Scan every file in *skill_dir* against the active YARA rules. Returns []
    when the engine or rules are unavailable. Never raises."""
    if _yara is None:
        return []
    rules = _compiled()
    if rules is None:
        return []
    skill_dir = Path(skill_dir)
    out: list[Finding] = []
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file():
            continue
        try:
            matches = rules.match(data=p.read_bytes()[:_MAX_BYTES])
        except Exception:  # noqa: BLE001 — a per-file failure is non-fatal
            continue
        rel = str(p.relative_to(skill_dir))
        for m in matches:
            sev = str((m.meta or {}).get("severity", "dangerous"))
            out.append(Finding("yara_signature", sev, rel, f"YARA match: {m.rule}"))
    return out
