"""Workspace ↔ FTS5 indexer.

Per `docs/memory/02_indexing.md` §6: the indexer is the bridge between
the markdown source-of-truth under ``memory/`` and the FTS5 sqlite
database at ``.durin/index/fts.sqlite``.

Two surfaces:

  - :func:`rebuild_fts_index` — wipes the index and re-derives every
    row from ``walk_memory``. Called by ``durin reindex`` and by the
    schema-version-mismatch recovery path.
  - :func:`reindex_one_file` — synchronous re-index of a single
    ``.md`` after a tool writes (memory_store, memory_ingest, Dream
    apply). Skipped silently when the path is outside ``memory/`` or
    under ``memory/archive/`` / ``memory/pending/``.

The vector index (LanceDB) is handled separately in
``durin.memory.vector_index``. Both stay in sync because the writes
fan out at the tool layer (re-index-on-write hooks); this module
focuses on the lexical side only.

Text composition (doc 02 §5.2):

  - Entity pages: ``name`` + ``aliases`` + rendered frontmatter +
    ``body`` (full).
  - Entries: ``headline`` + ``summary`` + ``entities_list`` +
    ``body`` (full).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from durin.memory.entity_page import EntityPage
from durin.memory.fts_index import FTSIndex
from durin.memory.paths import walk_memory
from durin.memory.storage import load_entry

__all__ = [
    "IndexStats",
    "detect_index_staleness",
    "rebuild_fts_index",
    "reindex_one_file",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexStats:
    """Result of a bulk rebuild — used by `durin reindex` CLI."""

    indexed: int
    errors: int


def rebuild_fts_index(workspace: Path) -> IndexStats:
    """Wipe and re-derive every FTS5 row from disk.

    Errors per file are logged + counted; the rebuild continues so a
    single bad file can't abort the whole pass. Fires
    ``memory.index.rebuild`` at end with the totals.
    """
    workspace = Path(workspace)
    indexed = 0
    errors = 0
    t0 = time.perf_counter()
    with FTSIndex.open(workspace) as idx:
        idx.clear()
        for md_path in walk_memory(workspace, include_archive=False):
            try:
                payload = _payload_for(workspace, md_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "indexer: skipping %s: %s", md_path, exc,
                )
                errors += 1
                continue
            if payload is None:
                continue
            try:
                idx.upsert(**payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "indexer: upsert %s failed: %s", md_path, exc,
                )
                errors += 1
                continue
            indexed += 1
    _emit_rebuild(target="fts", indexed=indexed, errors=errors,
                  duration_ms=(time.perf_counter() - t0) * 1000.0)
    return IndexStats(indexed=indexed, errors=errors)


def _emit_rebuild(
    *, target: str, indexed: int, errors: int, duration_ms: float,
) -> None:
    """Best-effort telemetry — never raises."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(
            "memory.index.rebuild",
            {
                "target": target,
                "indexed": indexed,
                "errors": errors,
                "duration_ms": duration_ms,
            },
        )
    except Exception:  # pragma: no cover
        pass


def _emit_write(*, uri: str, op: str, index: str = "fts") -> None:
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(
            "memory.index.write",
            {"uri": uri, "op": op, "index": index},
        )
    except Exception:  # pragma: no cover
        pass


def _emit_staleness(*, uri: str, reason: str) -> None:
    """Used by the health-check cron + the indexer's drift detection."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(
            "memory.index.staleness_detected",
            {"uri": uri, "reason": reason},
        )
    except Exception:  # pragma: no cover
        pass


def detect_index_staleness(workspace: Path) -> list[dict]:
    """Compare on-disk markdown to the FTS index and report drift.

    Three failure modes (per doc 07 §9.3):

    - ``missing_row``: a `.md` file exists under `memory/` but has no
      row in `fts_meta`.
    - ``mtime_lag``: the file's current `mtime` is later than the
      indexer's recorded value.
    - ``row_for_missing_file``: the index has a row whose `path`
      points at a `.md` that's no longer on disk.

    Emits one ``memory.index.staleness_detected`` event per detected
    issue. Returns the issues list so callers (health-check cron,
    `durin memory stats`) can act on them.
    """
    workspace = Path(workspace)
    issues: list[dict] = []

    fs_files: dict[str, float] = {}
    for md in walk_memory(workspace, include_archive=False):
        uri = _uri_for(workspace, md) or md.stem
        fs_files[uri] = md.stat().st_mtime

    with FTSIndex.open(workspace) as idx:
        seen_in_index: set[str] = set()
        for uri, indexed_mtime in idx.known_uris():
            seen_in_index.add(uri)
            current = fs_files.get(uri)
            if current is None:
                _emit_staleness(uri=uri, reason="row_for_missing_file")
                issues.append({"uri": uri, "reason": "row_for_missing_file"})
                continue
            if current > indexed_mtime:
                _emit_staleness(uri=uri, reason="mtime_lag")
                issues.append({"uri": uri, "reason": "mtime_lag"})

    for uri in fs_files:
        if uri not in seen_in_index:
            _emit_staleness(uri=uri, reason="missing_row")
            issues.append({"uri": uri, "reason": "missing_row"})

    return issues


def reindex_one_file(workspace: Path, md_path: Path) -> None:
    """Synchronous re-index for one file. Called after every write.

    No-ops when:
    - The path is not under ``<workspace>/memory/``.
    - The path is under ``memory/archive/`` or ``memory/pending/``.
    - The file disappeared between the write and this call (we
      delete the row instead).
    """
    workspace = Path(workspace)
    md_path = Path(md_path)
    memory_root = workspace / "memory"
    try:
        rel = md_path.relative_to(memory_root)
    except ValueError:
        return
    parts = rel.parts
    if parts and parts[0] in ("archive", "pending"):
        return
    if md_path.suffix != ".md":
        return
    with FTSIndex.open(workspace) as idx:
        if not md_path.is_file():
            uri = _uri_for(workspace, md_path)
            if uri is not None:
                idx.delete_by_uri(uri)
                _emit_write(uri=uri, op="delete")
            return
        try:
            payload = _payload_for(workspace, md_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "indexer: skip incremental %s: %s", md_path, exc,
            )
            return
        if payload is None:
            return
        try:
            idx.upsert(**payload)
            _emit_write(uri=payload["uri"], op="upsert")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "indexer: incremental upsert %s failed: %s",
                md_path, exc,
            )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _payload_for(workspace: Path, md_path: Path) -> Optional[dict]:
    """Derive the upsert payload for one file. Returns ``None`` if the
    file isn't an indexable shape (e.g. malformed entity page)."""
    rel_path = md_path.relative_to(workspace).as_posix()
    parts = md_path.relative_to(workspace / "memory").parts
    if not parts:
        return None
    mtime = md_path.stat().st_mtime

    if parts[0] == "entities":
        page = EntityPage.from_file(md_path)
        if page is None:
            return None
        slug = md_path.stem
        uri = f"{page.type}:{slug}"
        text = _entity_text(page)
        return {
            "uri": uri,
            "path": rel_path,
            "type_": "entity",
            "entity_type": page.type,
            "text": text,
            "mtime": mtime,
        }

    # Entry classes: stable / episodic / corpus / pending (but pending
    # is excluded by walk_memory; defensive check kept).
    class_name = parts[0]
    if class_name in ("pending", "archive"):
        return None
    # Let load_entry's parse errors propagate — the caller counts them
    # as `errors` so `IndexStats` reflects the corruption surface.
    entry = load_entry(md_path)
    return {
        "uri": entry.id,
        "path": rel_path,
        "type_": class_name,
        "entity_type": None,
        "text": _entry_text(entry),
        "mtime": mtime,
    }


def _uri_for(workspace: Path, md_path: Path) -> Optional[str]:
    """Best-effort URI derivation for a deleted file (no file content
    available). For entity pages the URI is ``<type>:<slug>``; for
    entries it's the bare ``id`` which equals the filename stem."""
    try:
        parts = md_path.relative_to(workspace / "memory").parts
    except ValueError:
        return None
    if not parts:
        return None
    if parts[0] == "entities" and len(parts) >= 3:
        type_ = parts[1]
        slug = md_path.stem
        return f"{type_}:{slug}"
    # For entries, the id == the filename stem by convention
    # (see durin.memory.storage); the indexer trusts that contract.
    return md_path.stem


def _entity_text(page: EntityPage) -> str:
    """Compose the BM25 text for an entity page (doc 02 §5.2)."""
    parts: list[str] = [page.name]
    if page.aliases:
        parts.append(" ".join(page.aliases))
    if page.attributes:
        parts.append(_render_attributes(page.attributes))
    if page.relations:
        parts.append(_render_relations(page.relations))
    if page.body:
        parts.append(page.body)
    return "\n".join(p for p in parts if p)


def _entry_text(entry) -> str:  # type: ignore[no-untyped-def]
    """Compose the BM25 text for one memory entry."""
    parts: list[str] = [entry.headline or ""]
    if entry.summary:
        parts.append(entry.summary)
    if entry.entities:
        parts.append(" ".join(entry.entities))
    if entry.body:
        parts.append(entry.body)
    return "\n".join(p for p in parts if p)


def _render_attributes(attrs: dict) -> str:
    """Flatten attributes into a single string for BM25 indexing."""
    lines: list[str] = []
    for k, v in attrs.items():
        if isinstance(v, (str, int, float)):
            lines.append(f"{k}: {v}")
        elif isinstance(v, list):
            lines.append(f"{k}: " + " ".join(str(x) for x in v))
        elif isinstance(v, dict):
            lines.append(
                f"{k}: " + " ".join(f"{sk}={sv}" for sk, sv in v.items())
            )
    return "\n".join(lines)


def _render_relations(rels: list) -> str:
    """Flatten relations into a single string for BM25 indexing."""
    lines: list[str] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        to = r.get("to", "")
        rel_type = r.get("type", "")
        lines.append(f"{rel_type} {to}")
    return " ".join(lines)
