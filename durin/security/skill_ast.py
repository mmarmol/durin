"""Stdlib-AST behavioral scan for bundled Python scripts (spec §4.c).

Catches dangerous call shapes that regex over/under-matches. Python-only; ``.sh``
and other languages stay regex-covered in ``skill_scan``. Never raises — a syntax
error yields one ``caution`` finding. Findings feed the human gate, not a block.
"""
from __future__ import annotations

import ast

from durin.security.skill_scan import Finding

# Dotted call names that are dangerous regardless of args. ``subprocess.*`` is
# deliberately NOT here: a plain ``subprocess.run`` is common and benign — only
# ``shell=True`` is flagged, by the dedicated check below.
_DANGER_CALLS = {
    "os.system": "os.system",
    "os.popen": "os.popen",
    "eval": "eval",
    "exec": "exec",
    "compile": "compile",
    "__import__": "__import__",
    "pickle.loads": "pickle.loads",
    "marshal.loads": "marshal.loads",
}


def _dotted(node: ast.AST) -> str:
    """Best-effort dotted name for a call target (``os.system``, ``eval``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def scan_python_ast(text: str, where: str) -> list[Finding]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [Finding("dangerous_code", "caution", where, "python file failed to parse")]
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name in _DANGER_CALLS:
            out.append(Finding("dangerous_code", "dangerous", where,
                               f"dangerous call {_DANGER_CALLS[name]}"))
        if name.startswith("subprocess.") and any(
            isinstance(k, ast.keyword) and k.arg == "shell"
            and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in node.keywords
        ):
            out.append(Finding("dangerous_code", "dangerous", where, "subprocess shell=True"))
    return out
