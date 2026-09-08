"""Memory keyed by the artifact the agent is about to use.

``memory_notes_for_path`` finds memory entries that mention a workspace
path (compaction summaries carry "Files/paths examined" trailers; episodic
notes cite paths) with the lexical index only — no embedding, no grep — so
a ``read_file`` can carry them at millisecond cost. ``entities_derived_from``
lists the entity pages a reference document was distilled into, so a drill
into the document also shows what memory already holds about it.
"""

from __future__ import annotations

import re
from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.fts_index import FTSIndex, fts_index_path
from durin.memory.lexical_search import lexical_search
from durin.memory.query_router import decide_lexical_route
from durin.memory.storage import load_entry

__all__ = ["entities_derived_from", "memory_notes_for_path"]

_NOTE_CLASSES = ("episodic", "stable", "session_summary")


def _enabled(default_limit: int) -> tuple[bool, int]:
    try:
        from durin.config.loader import load_config
        cfg = load_config().memory.artifact_recall
        return bool(cfg.enabled), int(cfg.max_notes)
    except Exception:  # noqa: BLE001 — no config file: defaults
        return True, default_limit


def memory_notes_for_path(workspace: Path, rel_path: str, *, limit: int | None = None) -> list[str]:
    """``- <uri> — <headline>`` lines for entries that mention ``rel_path``.

    Caller-provided limit overrides config; defaults to 3 when no limit is passed
    and no config exists.
    """
    enabled, cfg_limit = _enabled(3)
    effective_limit = limit if limit is not None else cfg_limit
    rel_path = (rel_path or "").strip()
    if not enabled or not rel_path or not fts_index_path(workspace).exists():
        return []
    try:
        with FTSIndex.open(workspace) as index:
            hits = lexical_search(
                index, decide_lexical_route(rel_path, keywords=rel_path), limit=effective_limit * 4,
            )
    except Exception:  # noqa: BLE001 — recall is a convenience, never an error
        return []
    # Build a regex that matches rel_path on a word boundary, allowing optional ./
    # prefix. This avoids false positives: "app.py" should not match "src/app.py".
    pattern = re.compile(
        r"(?<![\w/.\-])(?:\./)?" + re.escape(rel_path) + r"(?![\w/.\-])",
        re.IGNORECASE,
    )
    out: list[str] = []
    for hit in hits:
        if hit.type not in _NOTE_CLASSES:
            continue
        try:
            entry = load_entry(Path(workspace) / hit.path)
        except Exception:  # noqa: BLE001
            continue
        text = f"{entry.headline}\n{entry.body or entry.summary or ''}"
        if not pattern.search(text):
            continue  # FTS matched pieces of the path, not the exact path
        out.append(f"- {hit.uri} — {entry.headline}")
        if len(out) >= effective_limit:
            break
    return out


def entities_derived_from(workspace: Path, reference_ref: str, *, limit: int = 12) -> list[str]:
    """Entity refs whose ``derived_from`` names ``reference_ref``, alphabetical."""
    root = Path(workspace) / "memory" / "entities"
    if not root.is_dir():
        return []
    out: list[str] = []
    for md in sorted(root.rglob("*.md")):
        page = EntityPage.from_file(md)
        if page is None or reference_ref not in (page.derived_from or []):
            continue
        out.append(f"{md.parent.name}:{md.stem}")
        if len(out) >= limit:
            break
    return out
