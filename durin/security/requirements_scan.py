# durin/security/requirements_scan.py
"""Requirements extraction — 5-step heuristic pipeline run at scan time to
discover platform, binary, and env-var requirements for a skill directory.

Step 1 (frontmatter) is authoritative: declared values always win. Steps 2-5
only add items not already declared. See the spec for full design.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from durin.agent.skills_frontmatter import split_frontmatter

logger = logging.getLogger(__name__)


def extract_requirements(skill_dir: Path) -> dict:
    """Run the heuristic pipeline over *skill_dir*.

    Returns a manifest dict::

        {
          "platforms": {"value": [...], "inferred": bool},
          "bins": [{"name": str, "origin": str, "available": None}],
          "env": [{"name": str, "origin": str, "available": None}],
          "compatibility": str,
          "installable": bool,
          "blocked_by_platform": bool,
        }

    Never raises — degrades to an empty manifest on any error.
    """
    try:
        return _extract(Path(skill_dir))
    except Exception:  # noqa: BLE001 — never block the scan pipeline
        logger.warning("requirements_scan failed for %s, returning empty manifest", skill_dir)
        return {
            "platforms": {"value": [], "inferred": False},
            "bins": [],
            "env": [],
            "compatibility": "",
            "installable": False,
            "blocked_by_platform": False,
        }


def _extract(skill_dir: Path) -> dict:
    md = skill_dir / "SKILL.md"
    data: dict = {}
    body = ""
    if md.is_file():
        data, body = split_frontmatter(md.read_text(encoding="utf-8", errors="replace"))

    bins_seen: dict[str, str] = {}   # name -> origin
    env_seen: dict[str, str] = {}
    platforms: list[str] = []
    platforms_inferred = False
    compatibility = ""

    # --- Step 1: frontmatter (declared, authoritative) ---
    platforms = _step1_platforms(data)
    for b in _step1_bins(data):
        bins_seen[b] = "declared"
    for e in _step1_env(data):
        env_seen[e] = "declared"
    compatibility = str(data.get("compatibility") or "").strip()

    return {
        "platforms": {"value": platforms, "inferred": platforms_inferred},
        "bins": [{"name": n, "origin": o, "available": None} for n, o in bins_seen.items()],
        "env": [{"name": n, "origin": o, "available": None} for n, o in env_seen.items()],
        "compatibility": compatibility,
        "installable": False,
        "blocked_by_platform": False,
    }


# --- Step 1 helpers ---

def _step1_platforms(data: dict) -> list[str]:
    plats = data.get("platforms")
    if not plats:
        return []
    if isinstance(plats, str):
        plats = [plats]
    return [str(p).strip() for p in plats if str(p).strip()]


def _step1_bins(data: dict) -> list[str]:
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return []
    durin = meta.get("durin")
    if not isinstance(durin, dict):
        return []
    requires = durin.get("requires")
    if not isinstance(requires, dict):
        return []
    bins = requires.get("bins")
    if not isinstance(bins, list):
        return []
    return [str(b).strip() for b in bins if str(b).strip()]


def _step1_env(data: dict) -> list[str]:
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return []
    durin = meta.get("durin")
    if not isinstance(durin, dict):
        return []
    requires = durin.get("requires")
    if not isinstance(requires, dict):
        return []
    env = requires.get("env")
    if not isinstance(env, list):
        return []
    return [str(e).strip() for e in env if str(e).strip()]
