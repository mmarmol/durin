"""Deterministic pre-apply gate for a dream-proposed script edit.

A script edit (a new inline command, or new content for a script file) must
pass three checks, in order, before it is even eligible to be applied — first
failure wins and its output tail becomes the ``detail`` in the result:

1. Syntax — ``bash -n`` for a command or a ``.sh`` file, ``py_compile`` for a
   ``.py`` file. Any other extension (a shebang script, e.g. a Ruby or Perl
   file) has no generic syntax checker available here, so this check is
   skipped for it and syntax errors surface later at the smoke step instead.
2. Security — the proposed content is written into a scratch directory and
   run through the same skill scanner that gates dream-bundled skill scripts
   (``durin.security.skill_scan.scan_skill``); any verdict other than
   ``safe`` fails the gate with the scanner's own finding as the reason.
3. Smoke — the command/script is executed exactly once with empty stdin, a
   fresh scratch cwd, and the runner's clean env allowlist. This is
   deliberately narrower than the node's own env mode: the smoke run is not
   a real run (no DURIN_* variables are injected), it only proves the edit
   does not crash on startup. A plain non-zero exit is NOT a smoke failure —
   a gate command legitimately exiting 1 on empty input is a healthy check,
   not a broken one. The smoke step only fails on a spawn error, a timeout,
   or a startup-crash signature in stderr (missing interpreter, unresolved
   import, syntax error surfacing at runtime instead of parse time, etc).
"""

from __future__ import annotations

import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

from durin.security.skill_scan import scan_skill
from durin.workflow.script_runner import _CLEAN_ENV_ALLOWLIST, ScriptNodeRunner

# How much of a failing check's output to keep as the gate's `detail`.
_DETAIL_TAIL_CHARS = 500

# Case-insensitive substrings in smoke-run stderr that mean the process never
# got past startup — as opposed to a legitimate non-zero exit from a check
# that ran fine and simply decided to fail.
_STARTUP_CRASH_SIGNATURES = (
    "traceback",
    "syntaxerror",
    "command not found",
    "no such file or directory",
    "modulenotfounderror",
)


def _tail(text: str) -> str:
    return text.strip()[-_DETAIL_TAIL_CHARS:]


def _clean_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _CLEAN_ENV_ALLOWLIST if k in os.environ}


def _check_syntax(kind: str, suffix: str, script_path: Path) -> tuple[bool, str]:
    if kind == "command" or suffix == ".sh":
        proc = subprocess.run(
            ["bash", "-n", str(script_path)], capture_output=True, text=True,
            errors="replace")
        if proc.returncode != 0:
            return False, _tail(proc.stderr or proc.stdout)
        return True, ""
    if suffix == ".py":
        try:
            py_compile.compile(str(script_path), doraise=True)
        except py_compile.PyCompileError as exc:
            return False, _tail(str(exc))
        return True, ""
    return True, ""  # shebang script: no generic syntax checker here


def _check_security(scratch_dir: str) -> tuple[bool, str]:
    # A security gate fails CLOSED: a scanner crash means "not verified safe",
    # never a free pass — and never a raise out of the (bool, str) contract.
    try:
        report = scan_skill(Path(scratch_dir))
    except Exception as exc:  # noqa: BLE001 - fail closed on any scanner error
        return False, _tail(f"security scan failed: {exc}")
    if report.verdict == "safe":
        return True, ""
    reasons = "; ".join(f"{f.category}: {f.detail}" for f in report.findings)
    return False, _tail(reasons)


def _smoke_argv(kind: str, suffix: str, content: str, script_path: Path) -> list[str]:
    if kind == "command":
        return ["bash", "-c", content]
    if suffix == ".py":
        return [sys.executable, str(script_path)]
    if suffix == ".sh":
        return ["bash", str(script_path)]
    script_path.chmod(script_path.stat().st_mode | 0o111)
    return [str(script_path)]  # other-executable: must be runnable via its own shebang


def _check_smoke(argv: list[str], timeout: int) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as scratch_cwd:
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=scratch_cwd, env=_clean_env(), text=True,
                errors="replace", start_new_session=True,   # own process group, for a clean timeout kill
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return False, _tail(f"spawn error: {exc}")
        try:
            _, stderr = proc.communicate(input="", timeout=timeout)
        except subprocess.TimeoutExpired:
            ScriptNodeRunner._killpg(proc)
            return False, _tail(f"timed out after {timeout}s")
        stderr_tail = (stderr or "").strip()
        low = stderr_tail.lower()
        if any(sig in low for sig in _STARTUP_CRASH_SIGNATURES):
            return False, _tail(stderr_tail)
        return True, ""   # a plain non-zero exit with no crash signature is a healthy result


def precheck_script_edit(
    kind: str,
    content: str,
    *,
    filename: str | None = None,
    env: str = "clean",
    timeout: int = 5,
) -> tuple[bool, str]:
    """Run the syntax / security / smoke gate over a proposed script edit.

    `kind` is "command" (inline bash, run via `bash -c`) or "script_file"
    (`filename`'s extension picks the interpreter: .py, .sh, or an
    other-executable run directly via its own shebang). `env` is accepted for
    API symmetry with the node's own `env` field but is currently always
    treated as "clean" — the smoke run intentionally never adopts
    `env: "inherit"`, since it isn't a real run.
    """
    if kind not in ("command", "script_file"):
        raise ValueError(f"kind must be 'command' or 'script_file', got {kind!r}")

    with tempfile.TemporaryDirectory() as scratch_dir:
        if kind == "command":
            fname, suffix = "command.sh", ".sh"
        else:
            fname = filename or "script"
            suffix = Path(fname).suffix
        script_path = Path(scratch_dir) / fname
        script_path.write_text(content)

        # Unscannable content fails closed: without a recognized extension or a
        # shebang line the security scanner cannot classify the file as code and
        # would report "safe" without looking at it. Refuse instead of coasting.
        if kind == "script_file" and suffix not in (".py", ".sh") and not content.startswith("#!"):
            return False, ("unscannable script content: no recognized extension "
                           "(.py/.sh) and no shebang line — cannot be checked, so it "
                           "cannot be auto-applied")

        ok, detail = _check_syntax(kind, suffix, script_path)
        if not ok:
            return False, detail

        ok, detail = _check_security(scratch_dir)
        if not ok:
            return False, detail

        argv = _smoke_argv(kind, suffix, content, script_path)
        ok, detail = _check_smoke(argv, timeout)
        if not ok:
            return False, detail

    return True, ""
