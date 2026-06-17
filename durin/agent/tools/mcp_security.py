"""Vendor-agnostic, structural security scans for MCP server metadata and
spawn commands (SP-5).

Design rule (durin): NO natural-language / NLP token lists — those are
bypassable by paraphrase/translation and noisy on multilingual workloads
(see durin/security/secrets.py:362 and the project memory on heuristic
detectors). These scanners flag *structure* (forged turn delimiters,
runnable tool-call fences, opaque base64 blobs, URL+control co-occurrence,
and shell-interpreter + network-egress command shapes) that benign metadata
has no reason to contain. Findings are WARNING-level reason codes, never
hard blocks (except the command scan under an explicit ``refuse`` policy).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional, Tuple

from durin.security.network import _URL_RE  # reuse the existing URL matcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Injection-scan patterns (structural, language-agnostic)
# ---------------------------------------------------------------------------

# Forged role / chat-template delimiters. Fixed tokens, language-agnostic.
_ROLE_MARKERS = (
    re.compile(r"(?im)^\s*(system|assistant|developer|tool)\s*:"),
    re.compile(r"<\|\s*(im_start|im_end|system|assistant|user|end)\s*\|>"),
    re.compile(r"\[/?INST\]"),
    re.compile(r"(?im)^#{1,6}\s*instruction\s*:"),
)

# Runnable tool-call / function-call structures aimed at the model.
_TOOL_CALL_MARKERS = (
    re.compile(r"```\s*tool_call", re.IGNORECASE),
    re.compile(r"</?(tool_call|function_call|tool_use)\b", re.IGNORECASE),
    re.compile(r'```\s*json\s*\{\s*"name"\s*:', re.IGNORECASE),
)

# Opaque payloads: long contiguous base64 run, or a data: URI.
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_DATA_URI = re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,", re.IGNORECASE)


def _has_role_marker(text: str) -> bool:
    return any(p.search(text) for p in _ROLE_MARKERS)


def _has_tool_call(text: str) -> bool:
    return any(p.search(text) for p in _TOOL_CALL_MARKERS)


def _has_base64_blob(text: str) -> bool:
    return bool(_BASE64_BLOB.search(text) or _DATA_URI.search(text))


def scan_injection(text: object) -> list[str]:
    """Return structural injection reason codes for an untrusted metadata string.

    Codes: ``role_marker``, ``tool_call_fence``, ``base64_blob``,
    ``url_with_control``. Empty list = clean. Never raises; non-str -> [].
    """
    if not isinstance(text, str) or not text:
        return []
    codes: list[str] = []
    role = _has_role_marker(text)
    tool_call = _has_tool_call(text)
    if role:
        codes.append("role_marker")
    if tool_call:
        codes.append("tool_call_fence")
    if _has_base64_blob(text):
        codes.append("base64_blob")
    # A URL is only suspicious next to model-directed control structure.
    if _URL_RE.search(text) and (role or tool_call):
        codes.append("url_with_control")
    return codes


# ---------------------------------------------------------------------------
# Command exfil-blocklist (interpreter + egress shape)
# ---------------------------------------------------------------------------

_INTERPRETERS = frozenset(
    {
        "sh", "bash", "zsh", "dash", "fish", "ash", "ksh", "csh", "tcsh",
        "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    }
)

# Network-egress tooling (token-boundary matched, language-agnostic).
_EGRESS_TOOLS = (
    "curl", "wget", "nc", "ncat", "netcat", "telnet", "ftp", "tftp",
    "scp", "ssh", "socat", "iwr", "invoke-webrequest", "invoke-restmethod", "rsync",
)
_EGRESS_RE = re.compile(
    r"(?i)(?<![\w.-])(" + "|".join(re.escape(t) for t in _EGRESS_TOOLS) + r")(?![\w.-])"
)


def _interpreter_basename(command: str) -> str:
    return os.path.basename((command or "").strip()).lower()


def scan_spawn_command(command: str, args: object) -> list[str]:
    """Return reason codes for a suspicious stdio spawn command.

    Codes: ``interpreter_egress`` (shell interpreter whose inline args carry
    network-egress tooling), ``internal_url`` (any part targets a private/
    internal address). Empty list = clean. Pure-structural; no NL word lists.
    """
    codes: list[str] = []
    if not command:
        return codes
    arg_list = [str(a) for a in args] if isinstance(args, (list, tuple)) else []
    joined = " ".join([command, *arg_list]).strip()

    if _interpreter_basename(command) in _INTERPRETERS:
        inline = " ".join(arg_list)
        if _EGRESS_RE.search(inline):
            codes.append("interpreter_egress")

    from durin.security.network import contains_internal_url

    if contains_internal_url(joined):
        codes.append("internal_url")
    return codes


# ---------------------------------------------------------------------------
# OSV malware preflight (supply-chain / typosquat guard)
# ---------------------------------------------------------------------------

# Package runners and their ecosystems.
_PACKAGE_RUNNERS: dict[str, str] = {
    "npx": "npm",
    "npx.cmd": "npm",
    "npm": "npm",
    "pnpm": "npm",
    "bunx": "npm",
    "uvx": "PyPI",
    "uvx.cmd": "PyPI",
    "pipx": "PyPI",
}

# In-process cache: (package, ecosystem) → list[vuln_dict] | None
# None means "not yet queried"; [] means "queried, no malware".
_osv_cache: dict[tuple[str, str], list[dict]] = {}


def clear_osv_cache() -> None:
    """Clear the in-process OSV query cache (exposed for tests)."""
    _osv_cache.clear()


def check_package_for_malware(command: str, args: list) -> Optional[str]:
    """Check if an MCP stdio package has known malware advisories.

    Inspects *command* (e.g. ``npx``, ``uvx``) and *args* to extract the
    package name and ecosystem, then queries the OSV API for MAL-* advisories.

    Returns an error string if malware is found, or ``None`` if clean /
    unrecognised / fail-open. Never raises.
    """
    base = os.path.basename((command or "").strip()).lower()
    ecosystem = _PACKAGE_RUNNERS.get(base)
    if not ecosystem:
        return None

    package, version = _extract_package(args, ecosystem, base)
    if not package:
        return None

    cache_key = (package, ecosystem)
    if cache_key in _osv_cache:
        vulns = _osv_cache[cache_key]
    else:
        try:
            vulns = _query_osv(package, ecosystem, version)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug(
                "OSV malware check failed for %s/%s (allowing): %s",
                ecosystem, package, exc,
            )
            return None
        _osv_cache[cache_key] = vulns

    # Guard: only MAL-* ids constitute malware (defense-in-depth if _query_osv is replaced)
    mal_vulns = [v for v in vulns if v.get("id", "").startswith("MAL-")]
    if not mal_vulns:
        return None

    ids = ", ".join(v["id"] for v in mal_vulns[:3])
    return (
        f"MCP server refused: package '{package}' ({ecosystem}) has known "
        f"malware advisories: {ids}. Remove it from your MCP config or use "
        f"a trusted source."
    )


def _extract_package(
    args: list, ecosystem: str, runner: str
) -> Tuple[Optional[str], Optional[str]]:
    """Extract (package_name, version_or_None) from runner args.

    Skips leading flags (-y, --yes, etc.).  For ``npm exec`` the first
    positional is the sub-command ("exec") so we drop it first.
    """
    if not args:
        return None, None

    items = list(args)

    # ``npm exec -y pkg`` — drop the "exec" sub-command
    if runner == "npm" and items and not items[0].startswith("-"):
        items = items[1:]

    token: Optional[str] = None
    take_next = False
    for arg in items:
        if not isinstance(arg, str):
            continue
        if take_next:
            token = arg
            break
        if arg in ("--package", "-p"):
            take_next = True
            continue
        if arg.startswith("--package="):
            token = arg[len("--package="):]
            break
        if arg.startswith("-"):
            continue
        token = arg
        break

    if not token:
        return None, None

    if ecosystem == "npm":
        return _parse_npm_package(token)
    return _parse_pypi_package(token)


def _parse_npm_package(token: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse @scope/name@version or name@version."""
    if token.startswith("@"):
        m = re.match(r"^(@[^/]+/[^@]+)(?:@(.+))?$", token)
        if m:
            return m.group(1), m.group(2)
        return token, None
    if "@" in token:
        name, _, ver = token.rpartition("@")
        return name, (ver if ver != "latest" else None)
    return token, None


def _parse_pypi_package(token: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse name[extras]==version."""
    m = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?(?:==(.+))?$", token)
    if m:
        return m.group(1), m.group(2)
    return token, None


def _query_osv(
    package: str, ecosystem: str, version: Optional[str] = None
) -> list[dict]:
    """Return OSV MAL-* advisories as ``[{"id": ...}]``. Raises on any error.

    Delegates the HTTP query to the shared ``durin.security.osv`` helper; the
    dict shape is kept for ``check_package_for_malware``'s existing caller.
    """
    from durin.security.osv import query_malware

    return [{"id": mal_id} for mal_id in query_malware(package, ecosystem, version)]
