"""Workspace-level entity inventory helpers for prompt context.

Audit F17 (2026-05-28): ships the producer the Dream consolidator
needs for the `existing_uris` slot (doc 05 §5.1, doc 06 §2).

The slot was empty pre-F17 — `DreamConsolidator._build_prompt`
passed `existing_uris=()` and the LLM had no signal about the
workspace inventory, so it could create `person:marcelo_marmol`
when `person:marcelo` already existed in `memory/entities/`.

The producer walks `memory/entities/<type>/<slug>.md`, skips both
the legacy nested archive (`<canonical>/archive/...`) and the
top-level `memory/archive/entities/...`, sorts by file mtime
descending so the freshest entries surface first, and caps at the
configured number of URIs.

Output shape: `tuple[str, ...]` of `<type>:<slug>` strings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

__all__ = [
    "DEFAULT_EXISTING_URIS_CAP",
    "existing_uris_by_recent_mtime",
]


DEFAULT_EXISTING_URIS_CAP: int = 100


def _iter_entity_files(workspace: Path) -> Iterable[Path]:
    """Yield non-archive entity page paths under `memory/entities/`."""
    entities_root = workspace / "memory" / "entities"
    if not entities_root.is_dir():
        return
    for path in entities_root.rglob("*.md"):
        rel_parts = path.relative_to(entities_root).parts
        # Legacy nested archive: memory/entities/<type>/<canonical>/archive/...
        if "archive" in rel_parts:
            continue
        # Expect <type>/<slug>.md — anything deeper or shallower is
        # skipped (defensive against unexpected layout drift).
        if len(rel_parts) != 2:
            continue
        yield path


def existing_uris_by_recent_mtime(
    workspace: Path,
    *,
    cap: int = DEFAULT_EXISTING_URIS_CAP,
) -> tuple[str, ...]:
    """Return up to *cap* entity URIs ordered by recency (newest first).

    URIs use the canonical ``<type>:<slug>`` shape. Walking is
    best-effort: paths that disappear mid-walk or raise on `stat`
    are silently skipped so a flaky disk never breaks a Dream pass.
    """
    workspace = Path(workspace)
    # Top-level archive sits at memory/archive/, not under
    # memory/entities/, so the rglob in _iter_entity_files already
    # excludes it. The check stays defensive — if the layout shifts
    # and an entity ends up under archive/, we still skip it.
    rows: list[tuple[float, str]] = []
    for path in _iter_entity_files(workspace):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rel = path.relative_to(workspace / "memory" / "entities")
        type_ = rel.parts[0]
        slug = path.stem
        rows.append((mtime, f"{type_}:{slug}"))
    rows.sort(key=lambda r: r[0], reverse=True)
    return tuple(uri for _, uri in rows[: max(0, int(cap))])
