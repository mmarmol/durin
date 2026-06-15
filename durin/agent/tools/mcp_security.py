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

import os
import re

from durin.security.network import _URL_RE  # reuse the existing URL matcher

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
