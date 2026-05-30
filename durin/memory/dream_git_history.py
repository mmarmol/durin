"""Format `git log` of a single entity page for the `{recent_history}` slot.

Per `docs/architecture/memory/05_dream_cold_path.md` §5.1: the LLM sees the last
30 days of git commits to its target entity page so it can avoid
undoing its own recent decisions. The git source is the per-workspace
memory repo at ``<workspace>/memory/.git/``.

Output is a compact bulleted block ready for prompt injection:

    - 2026-05-26 (Dream): Update Marcelo's email
    - 2026-05-25 (Dream): Add spouse relation

Failures are silent: this is contextual prompt fuel, not load-bearing
logic. If git is missing, the repo doesn't exist, the entity has no
commits, or the call crashes — return ``"(no recent history)"`` and
let the consolidator pass continue.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

__all__ = ["format_recent_history"]

logger = logging.getLogger(__name__)

_SINCE = "30 days ago"
_NO_HISTORY = "(no recent history)"


def format_recent_history(workspace: Path, entity_ref: str) -> str:
    """Return a multi-line block summarising recent commits to the entity.

    *entity_ref* is the canonical URI (``<type>:<slug>``). Resolves to
    the relative path ``entities/<type>/<slug>.md`` inside the memory
    repo.
    """
    type_, _, slug = entity_ref.partition(":")
    if not type_ or not slug:
        return _NO_HISTORY

    memory_root = Path(workspace) / "memory"
    if not (memory_root / ".git").is_dir():
        return _NO_HISTORY

    rel_path = f"entities/{type_}/{slug}.md"
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={_SINCE}",
                "--no-color",
                # `%ad` short date, `%s` subject. Tab separator keeps
                # parsing simple without YAML-fragile chars.
                "--date=short",
                "--pretty=format:%ad\t%s",
                "--",
                rel_path,
            ],
            cwd=memory_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.debug("dream_git_history: git unavailable: %s", exc)
        return _NO_HISTORY
    except subprocess.SubprocessError as exc:
        logger.debug("dream_git_history: subprocess error: %s", exc)
        return _NO_HISTORY

    if result.returncode != 0:
        # `--` with a non-existent path is silent on stdout but the
        # call succeeds in modern git. Other return codes mean trouble
        # (corrupt repo, etc.) — bail.
        return _NO_HISTORY

    stdout = result.stdout.strip()
    if not stdout:
        return _NO_HISTORY

    lines: list[str] = []
    for raw_line in stdout.splitlines():
        if "\t" not in raw_line:
            continue
        date, subject = raw_line.split("\t", 1)
        lines.append(f"- {date}: {subject.strip()}")
    if not lines:
        return _NO_HISTORY
    return "\n".join(lines)
