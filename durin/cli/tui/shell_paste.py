"""Shell-paste pre-processor for the TUI input (D3.2).

Patterns the legacy CLI doesn't expose but Pi / Claude Code popularized:

- ``!cmd``  → run ``cmd`` in a subprocess; the stdout is concatenated
  into the user's message before publishing. Useful for "just send the
  agent the output of ``git status``".
- ``!!cmd`` → run ``cmd`` silently. The user message is suppressed —
  the agent sees nothing. Useful for "I want to run a shell command
  without involving the agent".

The handler returns ``(send, message)`` where ``send`` is ``False`` for
suppress-mode. Pure function so the TUI submission path can call it
unconditionally; if the text doesn't start with ``!`` it returns the
text untouched.

Security note: we never invoke a shell parser; we use ``subprocess.run``
with ``shell=True`` only because the value the user typed IS a shell
command. The user typed it themselves; this isn't a tool the agent
can call.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

__all__ = ["ShellPasteResult", "process_shell_paste"]


@dataclass(frozen=True)
class ShellPasteResult:
    """What the TUI should do with a user submission.

    - ``send=False, message=""`` → suppress. Don't publish anything to
      the agent (``!!cmd`` ran silently).
    - ``send=True, message=<text>`` → publish ``message`` as the user
      turn. For plain text it's the original; for ``!cmd`` it's
      ``"<original prefix>\\n\\n```\\n<stdout>\\n```"`` so the agent
      sees the command and its output.
    """

    send: bool
    message: str
    ran_command: str | None = None
    exit_code: int | None = None


_TIMEOUT_S = 30
_MAX_OUTPUT_CHARS = 32_000


def _run(cmd: str) -> tuple[int, str]:
    """Run ``cmd`` via the user's shell, capture combined stdout+stderr."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        return 124, f"[command timed out after {_TIMEOUT_S}s]\n{exc.stdout or ''}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[:_MAX_OUTPUT_CHARS] + f"\n…(truncated, {len(out) - _MAX_OUTPUT_CHARS} chars omitted)"
    return proc.returncode, out


def process_shell_paste(text: str) -> ShellPasteResult:
    """Apply the ``!`` / ``!!`` conventions to a user submission.

    Plain text (no leading ``!``) returns ``ShellPasteResult(send=True,
    message=text)`` unchanged.
    """
    stripped = text.lstrip()
    if not stripped.startswith("!"):
        return ShellPasteResult(send=True, message=text)

    if stripped.startswith("!!"):
        cmd = stripped[2:].strip()
        if not cmd:
            return ShellPasteResult(send=True, message=text)
        rc, _ = _run(cmd)
        return ShellPasteResult(send=False, message="", ran_command=cmd, exit_code=rc)

    # single `!`
    cmd = stripped[1:].strip()
    if not cmd:
        return ShellPasteResult(send=True, message=text)
    rc, output = _run(cmd)
    body = (
        f"Ran `{cmd}` (exit {rc}); output:\n\n"
        f"```\n{output.rstrip()}\n```"
    )
    return ShellPasteResult(send=True, message=body, ran_command=cmd, exit_code=rc)
