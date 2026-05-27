"""Hybrid commit-message post-processor.

The LLM emits a commit message inside the ``===COMMIT===`` section
with the format documented in ``durin/templates/dream/commit_format.md``:

    <subject ≤ 70 chars>

    <optional body>

    Sources: ...
    Cursor-after: ...
    Entities-touched: ...

Per `docs/memory/05_dream_cold_path.md` §11, the runner is responsible
for two final touches:

1. **Verify** the three LLM-supplied trailers exist. Missing ones are
   filled in from runner state (and a warning is logged by the
   caller).
2. **Append** ``Trigger:`` and ``Run-id:`` trailers — these are
   always known by the runner, never by the LLM, and the spec
   explicitly forbids the LLM from emitting them.

This module is pure text: no I/O, no telemetry, no logging. Caller
decides whether to warn / emit telemetry for missing trailers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

__all__ = ["CommitTrailers", "finalize_commit_message"]


@dataclass(frozen=True)
class CommitTrailers:
    """All trailer values the runner knows."""

    sources: list[str]
    cursor_after: str
    entities_touched: str
    trigger: str
    run_id: str


# Trailers in the order they should appear at the end of the commit
# message. This is the order downstream `git log --grep` recipes
# rely on; do not reshuffle without coordinating with `memory_history`.
_TRAILER_ORDER: tuple[str, ...] = (
    "Sources",
    "Cursor-after",
    "Entities-touched",
    "Trigger",
    "Run-id",
)


def finalize_commit_message(
    llm_message: str,
    *,
    trailers: CommitTrailers,
) -> str:
    """Return the final commit message string ready for ``git commit -m``."""
    # Strip any existing trailers from the LLM message — we'll
    # re-emit a canonical block. This handles three cases:
    #   - LLM omitted trailers → we add ours
    #   - LLM emitted some → we replace with canonical
    #   - LLM hallucinated Trigger/Run-id → we override with real
    head_lines = _strip_known_trailers(llm_message.rstrip("\n"))
    trailer_block = _render_trailer_block(trailers)
    if head_lines.strip():
        return f"{head_lines.rstrip()}\n\n{trailer_block}\n"
    # Empty / whitespace-only LLM output: the trailers stand alone.
    return trailer_block + "\n"


def _strip_known_trailers(message: str) -> str:
    """Drop any line that starts with one of the known trailer prefixes.

    Trailers may appear scattered through the body (some models put
    them between subject and body); we strip them wherever we find them
    and add a clean block at the end.
    """
    keep: list[str] = []
    for line in message.splitlines():
        stripped = line.lstrip()
        if any(stripped.startswith(f"{t}:") for t in _TRAILER_ORDER):
            continue
        keep.append(line)
    # Drop trailing blank lines we now have from stripped trailers.
    while keep and not keep[-1].strip():
        keep.pop()
    return "\n".join(keep)


def _render_trailer_block(trailers: CommitTrailers) -> str:
    lines: list[str] = [
        f"Sources: {_render_sources(trailers.sources)}",
        f"Cursor-after: {trailers.cursor_after}",
        f"Entities-touched: {trailers.entities_touched}",
        f"Trigger: {trailers.trigger}",
        f"Run-id: {trailers.run_id}",
    ]
    return "\n".join(lines)


def _render_sources(sources: Iterable[str]) -> str:
    cleaned = [s.strip() for s in sources if isinstance(s, str) and s.strip()]
    return ", ".join(cleaned)
