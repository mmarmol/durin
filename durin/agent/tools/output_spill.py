"""Tool output spill helper — Sprint A / T4.

When a tool produces output larger than its budget, spill the FULL content
to a file under ``<workspace>/.durin/spills/`` and return a truncated version
that references it. The model can recover anything it actually needs via
``read_file(path=<spill_path>)`` without contaminating context with the rest.

Inspired by OpenCode's ``TOOL_OUTPUT_MAX_CHARS`` (2000-char compaction cap in
``packages/opencode/src/session/compaction.ts``), adapted to be LIVE rather
than post-hoc: spill happens at the moment of overflow, not retroactively
during compaction. This keeps the model's context lean from the start.

See ``docs/architecture/loop.md`` §1.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from durin.utils.atomic_write import atomic_write_text

_SPILL_SUBDIR = ".durin/spills"


def _spill_root(workspace: Path | None) -> Path:
    """Resolve the directory where spills get written.

    Prefers ``<workspace>/.durin/spills`` so that ``ReadFileTool`` with
    ``restrict_to_workspace`` can read spills without extra allowed-dir wiring.
    Falls back to a fixed ``/tmp/durin_spills/`` when no workspace is available
    (tests, ad-hoc calls).
    """
    if workspace is not None:
        try:
            return Path(workspace).resolve() / _SPILL_SUBDIR
        except (OSError, RuntimeError):
            pass
    return Path("/tmp/durin_spills")


def _spill_filename(tool_name: str, content: str) -> str:
    """Deterministic-ish filename: tool + timestamp + content-hash prefix."""
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:10]
    ts = int(time.time())
    safe_tool = "".join(c if c.isalnum() else "_" for c in tool_name)[:32]
    return f"{safe_tool}_{ts}_{digest}.txt"


def truncate_with_spill(
    content: str,
    tool_name: str,
    workspace: Path | None,
    max_chars: int,
    *,
    head_ratio: float = 0.7,
    redact: Callable[[str], str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Truncate ``content`` if it exceeds ``max_chars``; spill original to disk.

    Returns ``(rendered_text, telemetry_dict)``. When no truncation is needed,
    returns ``content`` unchanged with ``spilled=False``. When spilled, the
    truncated rendering keeps ``head_ratio`` of the budget as head, the rest
    minus the footer as tail, and inserts a reference to the spill file.

    ``redact``, when given, is applied to ``content`` *before* the spill is
    written so secret values never reach the spill file on disk (A4). The
    short-circuit (no-spill) path skips it — that content is returned to the
    caller, which redacts it before it reaches the model.

    Spill write failures fall back to plain head/tail truncation (no spill ref)
    — the tool call must never break because a temp dir was un-writable.
    """
    n = len(content)
    if n <= max_chars:
        return content, {"spilled": False, "original_chars": n, "rendered_chars": n}

    # Redact before anything touches disk — closes the spill-before-redact
    # leak (A4). Recompute length so head/tail math uses the redacted text.
    if redact is not None:
        content = redact(content)
        n = len(content)

    root = _spill_root(workspace)
    spill_path: Path | None = None
    spill_error: str | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        spill_path = root / _spill_filename(tool_name, content)
        atomic_write_text(spill_path, content)
    except Exception as e:
        spill_error = str(e)[:80]
        spill_path = None

    head_budget = max(0, int(max_chars * head_ratio))
    # Reserve room for the footer (~200 chars) within the budget.
    tail_budget = max(0, max_chars - head_budget - 200)
    head = content[:head_budget]
    tail = content[-tail_budget:] if tail_budget > 0 else ""
    omitted = n - len(head) - len(tail)

    if spill_path is not None:
        # Use absolute path for the reference so it's unambiguous.
        ref = str(spill_path)
        footer = (
            f"\n\n... ({omitted:,} chars omitted; full output spilled to disk) ...\n"
            f"Full output: {ref}\n"
            f"Read with: read_file(path={ref!r})\n\n"
        )
    else:
        footer = (
            f"\n\n... ({omitted:,} chars omitted; spill write failed: "
            f"{spill_error or 'unknown'}) ...\n\n"
        )

    rendered = head + footer + tail
    telemetry = {
        "spilled": spill_path is not None,
        "original_chars": n,
        "rendered_chars": len(rendered),
        "spill_path": str(spill_path) if spill_path else None,
        "spill_error": spill_error,
    }
    return rendered, telemetry
