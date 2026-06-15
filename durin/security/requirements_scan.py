# durin/security/requirements_scan.py
"""Requirements extraction — 5-step heuristic pipeline run at scan time to
discover platform, binary, and env-var requirements for a skill directory.

Step 1 (frontmatter) is authoritative: declared values always win. Steps 2-5
only add items not already declared. See the spec for full design.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
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
    for b in _step1_allowed_tools(data):
        if b not in bins_seen:
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


_ALLOWED_TOOL_RE = re.compile(r"\w+\(([\w.-]+)[:\s)]")


def _step1_allowed_tools(data: dict) -> list[str]:
    """Extract binary names from the root-level ``allowed-tools`` frontmatter field.

    Entries like ``Bash(agent-browser:*)`` yield ``agent-browser``.
    Shell builtins and agent framework tools are filtered out."""
    raw = data.get("allowed-tools")
    if not isinstance(raw, str):
        return []
    out: list[str] = []
    for m in _ALLOWED_TOOL_RE.finditer(raw):
        tool = m.group(1).strip()
        if tool and tool not in _SHELL_BUILTINS:
            out.append(tool)
    return out


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
            if tool and tool not in ("bash", "sh", "zsh", "env"):
                out.append(tool)
        for m in re.finditer(r"^#!/(?:usr/)?bin/(\S+)", text, re.MULTILINE):
            tool = m.group(1).strip()
            if tool and tool not in ("bash", "sh", "zsh", "env"):
                out.append(tool)
    return out


# --- Step 3: script command invocations ---
_CMD_PATTERNS = [
    re.compile(r'subprocess\.(?:run|call|Popen|check_output|check_call)\s*\(\s*\[?\s*["\']([a-zA-Z0-9_-]+)["\']'),
    re.compile(r'shell\s*\(\s*["\']([a-zA-Z0-9_-]+)\s'),
    re.compile(r'\bexec\s*\(\s*["\']([a-zA-Z0-9_-]+)\s'),
    re.compile(r'^\s*([a-zA-Z0-9_-]+)\s', re.MULTILINE),
]
_SHELL_BUILTINS = {"bash", "sh", "zsh", "dash", "fish", "ksh", "tcsh", "csh",
                   "echo", "cd", "ls", "mkdir", "rm", "cp", "mv", "cat", "grep", "sed",
                   "awk", "export", "source", "set", "exit", "if", "then", "fi", "for",
                   "do", "done", "while", "return", "function", "sudo", "env", "nohup",
                   "kill", "date", "sleep", "tr", "ps", "sort", "head", "tail", "wc",
                   "which", "dirname", "basename", "test", "true", "false", "case", "esac",
                   "elif", "else", "shift", "unset", "local", "declare", "readonly",
                   "trap", "wait", "jobs", "bg", "fg", "disown", "pushd", "popd"}
_RUNTIME_RE = re.compile(
    r'\b(node|npx|python3|python|ruby|perl|java|go|cargo|rustc|gcc|make|cmake)\b'
)


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
                if tool and tool not in _SHELL_BUILTINS:
                    out.append(tool)
        if p.suffix == ".sh":
            for m in _RUNTIME_RE.finditer(text):
                tool = m.group(1)
                if tool and tool not in _SHELL_BUILTINS:
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


# --- Display-model resolver ---

_PLATFORM_ALIASES = {"darwin": "macos", "win32": "windows"}
_PLATFORM_INSTALL_KINDS = {"macos": ("brew", "cask"), "linux": ("apt",)}


def _current_platform() -> str:
    import sys
    plat = sys.platform
    return _PLATFORM_ALIASES.get(plat, plat)


def resolve_display(manifest: dict, *, platform: str | None = None,
                    catalog: dict | None = None) -> dict:
    """Transform the internal manifest into the API display model.

    - Strips all ``origin`` fields.
    - Resolves ``available`` via live ``shutil.which`` / ``os.environ``.
    - Computes ``installable`` + ``install_spec`` per bin using catalog.
    - Computes ``platform_ok``.
    """
    catalog = catalog or {}
    if platform is None:
        platform = _current_platform()

    plats = manifest.get("platforms", {}).get("value", [])
    if not plats:
        platform_ok = True
    else:
        platform_ok = platform in plats

    valid_kinds = _PLATFORM_INSTALL_KINDS.get(platform, ())
    bins_out = []
    for b in manifest.get("bins", []):
        name = b["name"]
        available = shutil.which(name) is not None
        entry: dict = {"name": name, "available": available}
        cat_entry = catalog.get(name)
        if cat_entry:
            primary = cat_entry.get("primary", {})
            if primary.get("kind") in valid_kinds:
                entry["installable"] = True
                entry["install_spec"] = f"{primary['kind']}: {primary['value']}"
            else:
                for alt in cat_entry.get("alternatives", []):
                    if alt.get("kind") in valid_kinds:
                        entry["installable"] = True
                        entry["install_spec"] = f"{alt['kind']}: {alt['value']}"
                        break
                else:
                    entry["installable"] = False
        else:
            entry["installable"] = False
        bins_out.append(entry)

    env_out = []
    for e in manifest.get("env", []):
        env_out.append({"name": e["name"], "available": bool(os.environ.get(e["name"]))})

    return {
        "platforms": plats,
        "platform_ok": platform_ok,
        "bins": bins_out,
        "env": env_out,
        "compatibility": manifest.get("compatibility", ""),
    }
