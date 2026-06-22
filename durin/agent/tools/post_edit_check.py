"""Post-edit check: run a configurable linter after write_file/edit_file.

Closes the feedback loop opencode gets from LSP diagnostics, without an LSP
client: after a successful write, the file is handed to a per-language
checker subprocess (extension → command template, user-configurable, open
vocabulary) and the findings are appended to the tool result so the model
sees breakage immediately instead of discovering it at run time.

Graceful skip everywhere — disabled config, unknown extension, missing
binary, timeout, checker crash. A check failure must NEVER break an edit.
See docs/internals/loop.md §"Tool write durability".
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import time
from pathlib import Path

from loguru import logger
from pydantic import Field

from durin.agent.tools._telemetry import emit_tool_event
from durin.config.schema import Base
from durin.utils.subprocess_cleanup import aclose_subprocess

_DEFAULT_CHECKERS: dict[str, str] = {
    # ruff is durin's own linter; skipped silently when not installed.
    "py": "ruff check --output-format=concise {file}",
}


class PostEditCheckConfig(Base):
    """Configuration for post-edit linter checks."""

    enable: bool = True
    timeout_s: int = 10
    max_lines: int = 20
    # extension (no dot) → command template; "{file}" is replaced with the
    # edited file's path. The command runs WITHOUT a shell.
    checkers: dict[str, str] = Field(
        default_factory=lambda: dict(_DEFAULT_CHECKERS)
    )


async def run_post_edit_check(
    fp: Path, config: PostEditCheckConfig | None,
) -> str | None:
    """Run the configured checker for *fp*; return a findings block or None.

    None means "nothing to report": clean run, no checker configured for
    this extension, checker binary missing, check disabled, or the checker
    itself failed/timed out (logged, never raised).
    """
    if config is None or not config.enable:
        return None
    ext = fp.suffix.lstrip(".").lower()
    template = config.checkers.get(ext)
    if not template:
        return None

    try:
        parts = shlex.split(template)
    except ValueError:
        logger.debug("post-edit check: unparseable template for .{}", ext)
        return None
    if not parts:
        return None
    binary = shutil.which(parts[0])
    if binary is None:
        return None
    argv = [binary] + [
        str(fp) if token == "{file}" else token for token in parts[1:]
    ]

    started = time.monotonic()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=config.timeout_s,
        )
    except (OSError, asyncio.TimeoutError) as e:
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await aclose_subprocess(proc)
        logger.debug("post-edit check failed for {}: {}", fp, e)
        emit_tool_event("tool.post_edit_check", {
            "path": str(fp),
            "checker": parts[0],
            "exit_code": None,
            "issue_lines": 0,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "skipped_reason": "timeout" if isinstance(e, asyncio.TimeoutError) else "os_error",
        })
        return None

    await aclose_subprocess(proc)
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    lines = [
        line for line in stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    emit_tool_event("tool.post_edit_check", {
        "path": str(fp),
        "checker": parts[0],
        "exit_code": proc.returncode,
        "issue_lines": len(lines) if proc.returncode != 0 else 0,
        "duration_ms": duration_ms,
    })
    if proc.returncode == 0 or not lines:
        return None

    shown = lines[: config.max_lines]
    more = len(lines) - len(shown)
    suffix = f"\n... and {more} more" if more > 0 else ""
    body = "\n".join(shown)
    return (
        f"\n\n(post-edit check '{parts[0]}' reported issues:)\n{body}{suffix}"
    )
