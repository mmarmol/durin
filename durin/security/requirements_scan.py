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


def extract_requirements(skill_dir: Path, *, workspace: Path | None = None,
                         llm_tools: list[str] | None = None) -> dict:
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
        return _extract(Path(skill_dir), workspace=workspace, llm_tools=llm_tools or [])
    except Exception:  # noqa: BLE001 — never block the scan pipeline
        logger.warning("requirements_scan failed for %s, returning empty manifest", skill_dir)
        return {
            "platforms": {"value": [], "inferred": False},
            "bins": [],
            "env": [],
            "compatibility": "",
            "installable": False,
            "blocked_by_platform": False,
            "platform_conflict": False,
        }


def _extract(skill_dir: Path, *, workspace: Path | None = None,
             llm_tools: list[str] | None = None) -> dict:
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

    # --- Steps 2-3: script analysis (only adds non-declared bins) ---
    scripts_dir = skill_dir / "scripts"
    if scripts_dir.is_dir():
        for b in _step2_shebang_bins(scripts_dir):
            if b not in bins_seen:
                bins_seen[b] = "heuristic:script"
        for b in _step3_script_bins(scripts_dir):
            if b not in bins_seen:
                bins_seen[b] = "heuristic:script"

    # --- Step 4: body backtick-quoted tools (context + catalog gated) ---
    from durin.security.tool_catalog import load_catalog
    catalog = load_catalog(workspace)
    for b in _step4_body_bins(body, catalog):
        if b not in bins_seen:
            bins_seen[b] = "heuristic:body"

    # --- LLM-discovered tools (lowest priority, merged after all heuristics) ---
    for tool in (llm_tools or []):
        if tool not in bins_seen:
            bins_seen[tool] = "llm"

    # --- Step 5: platform inference from install specs ---
    if not platforms:
        inferred_plats = _step5_inferred_platforms(data)
        if inferred_plats:
            platforms = inferred_plats
            platforms_inferred = True

    # --- Conflict detection ---
    platform_conflict = False
    if not platforms_inferred and platforms:
        inferred = _step5_inferred_platforms(data)
        if inferred and not any(p in platforms for p in inferred):
            platform_conflict = True

    return {
        "platforms": {"value": platforms, "inferred": platforms_inferred},
        "bins": [{"name": n, "origin": o, "available": None} for n, o in bins_seen.items()],
        "env": [{"name": n, "origin": o, "available": None} for n, o in env_seen.items()],
        "compatibility": compatibility,
        "installable": False,
        "blocked_by_platform": False,
        "platform_conflict": platform_conflict,
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


# --- Step 2: script shebangs ---
def _step2_shebang_bins(scripts_dir: Path) -> list[str]:
    """Extract tools from shebangs: #!/usr/bin/env <tool>."""
    out: list[str] = []
    for p in sorted(scripts_dir.rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^#!.*?env\s+(\S+)", text, re.MULTILINE):
            tool = m.group(1).strip()
            if tool and tool not in ("bash", "sh", "python", "python3", "node", "ruby", "perl", "zsh"):
                out.append(tool)
        for m in re.finditer(r"^#!/(?:usr/)?bin/(\S+)", text, re.MULTILINE):
            tool = m.group(1).strip()
            if tool and tool not in ("bash", "sh", "env", "python", "python3", "node", "ruby", "perl", "zsh"):
                out.append(tool)
    return out


# --- Step 3: script command invocations ---
_CMD_PATTERNS = [
    re.compile(r'subprocess\.(?:run|call|Popen|check_output|check_call)\s*\(\s*\[?\s*["\']([a-zA-Z0-9_-]+)["\']'),
    re.compile(r'shell\s*\(\s*["\']([a-zA-Z0-9_-]+)\s'),
    re.compile(r'\bexec\s*\(\s*["\']([a-zA-Z0-9_-]+)\s'),
    re.compile(r'^\s*([a-zA-Z0-9_-]+)\s', re.MULTILINE),
]
_INTERPRETER_BINS = {"bash", "sh", "python", "python3", "node", "ruby", "perl", "zsh",
                     "echo", "cd", "ls", "mkdir", "rm", "cp", "mv", "cat", "grep", "sed",
                     "awk", "export", "source", "set", "exit", "if", "then", "fi", "for",
                     "do", "done", "while", "return", "function", "sudo"}


def _step3_script_bins(scripts_dir: Path) -> list[str]:
    """Extract tools invoked via subprocess/shell/exec in script files."""
    out: list[str] = []
    for p in sorted(scripts_dir.rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat in _CMD_PATTERNS[:3]:
            for m in pat.finditer(text):
                tool = m.group(1).strip()
                if tool and tool not in _INTERPRETER_BINS:
                    out.append(tool)
    return out


# --- Step 4: body backtick-quoted tools (context + catalog gated) ---

_ACTION_VERBS = ("run", "execute", "use", "call", "install", "requires", "needs", "invoke")
_BACKTICK_RE = re.compile(r"`([a-zA-Z0-9_-]+)`")


def _step4_body_bins(body: str, catalog: dict) -> list[str]:
    """Extract backtick-quoted tool names from SKILL.md body that appear within
    ±5 words of an action verb AND exist in the tool catalog. Both gates must pass.
    Backticks inside fenced code blocks are ignored."""
    cleaned = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    out: list[str] = []
    for m in _BACKTICK_RE.finditer(cleaned):
        tool = m.group(1).strip()
        if tool not in catalog:
            continue
        start = max(0, m.start() - 40)
        end = min(len(cleaned), m.end() + 40)
        window = cleaned[start:end].lower()
        if not any(verb in window for verb in _ACTION_VERBS):
            continue
        if tool not in out:
            out.append(tool)
    return out


# --- Step 5: platform inference from install specs ---

_INSTALL_PLATFORM_MAP = {
    "brew": "macos",
    "cask": "macos",
    "apt": "linux",
}


def _step5_inferred_platforms(data: dict) -> list[str]:
    """Infer platforms from declared install specs (brew→macOS, apt→Linux)."""
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return []
    inferred: list[str] = []
    for blob in meta.values():
        if not isinstance(blob, dict):
            continue
        specs = blob.get("install")
        if not isinstance(specs, list):
            continue
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get("kind", ""))
            plat = _INSTALL_PLATFORM_MAP.get(kind)
            if plat and plat not in inferred:
                inferred.append(plat)
    return inferred
