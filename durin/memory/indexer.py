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
    "ensure_index_fresh",
    "rebuild_fts_index",
    "reindex_one_file",
]


# Module-level cache of workspaces we've already freshness-checked in
# this process. Prevents re-walking memory/ on every tool init.
_FRESHNESS_CHECKED: set[str] = set()


def _RESET_FRESHNESS_CACHE_FOR_TESTS() -> None:  # noqa: N802
    """Test helper — never call from production code."""
    _FRESHNESS_CHECKED.clear()


def ensure_index_fresh(workspace: Path) -> dict:
    """Auto-rebuild the FTS index when the on-disk schema_version
    differs from :data:`durin.memory.index_meta.CURRENT_SCHEMA_VERSION`.

    Idempotent within a single process — the second call for the same
    workspace short-circuits via a module-level cache. Different
    workspaces stay isolated.

    Returns a dict ``{"rebuilt": bool, "reason": str}`` so callers can
    log the outcome. ``reason`` values:

    - ``"cached"``  — already checked this workspace this process.
    - ``"no_memory_dir"`` — workspace has no ``memory/`` yet.
    - ``"missing_meta"`` — first run; meta.json absent.
    - ``"schema_mismatch"`` — on-disk version differs from code.
    - ``"current"``  — meta matches; no work done.
    """
    from durin.memory.index_meta import (
        CURRENT_SCHEMA_VERSION,
        IndexMeta,
        load_index_meta,
        save_index_meta,
    )

    key = str(Path(workspace).resolve())
    if key in _FRESHNESS_CHECKED:
        return {"rebuilt": False, "reason": "cached"}
    _FRESHNESS_CHECKED.add(key)

    workspace = Path(workspace)
    if not (workspace / "memory").is_dir():
        return {"rebuilt": False, "reason": "no_memory_dir"}

    meta = load_index_meta(workspace)
    if meta is None:
        reason = "missing_meta"
    elif meta.schema_version != CURRENT_SCHEMA_VERSION:
        reason = "schema_mismatch"
    else:
        return {"rebuilt": False, "reason": "current"}

    # Rebuild + persist new meta. Best-effort: a rebuild failure logs
    # but doesn't propagate — the search pipeline's graceful
    # degradation handles a stale/empty index from there.
    try:
        stats = rebuild_fts_index(workspace)
        _emit_rebuild(
            target="fts",
            indexed=stats.indexed,
            errors=stats.errors,
            duration_ms=0.0,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ensure_index_fresh: rebuild failed for %s: %s",
            workspace, exc,
        )
        return {"rebuilt": False, "reason": f"error: {exc}"}

    # Bump meta to the current version. Preserve any previous
    # embedding_model_id if present, else write an empty marker.
    existing_model = meta.embedding_model_id if meta else ""
    save_index_meta(
        workspace,
        IndexMeta(
            schema_version=CURRENT_SCHEMA_VERSION,
            embedding_model_id=existing_model,
        ),
    )
    return {"rebuilt": True, "reason": reason}

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
    *,
    target: str,
    indexed: int,
    errors: int,
    duration_ms: float,
    reason: str | None = None,
) -> None:
    """Best-effort telemetry — never raises.

    ``reason`` is included when the rebuild was triggered by something
    other than an explicit CLI call (e.g. ``schema_mismatch`` from
    :func:`ensure_index_fresh`).
    """
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        payload: dict = {
            "target": target,
            "indexed": indexed,
            "errors": errors,
            "duration_ms": duration_ms,
        }
        if reason:
            payload["reason"] = reason
        emit_tool_event("memory.index.rebuild", payload)
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
    # URI shape matches the grep/v1 path's `Result.uri` so cross-
    # source RRF can dedupe a hit that surfaces via both FTS and
    # grep. Without this prefix the same entry would appear twice
    # in the fused result list (once as "abc123" via FTS, once as
    # "memory/episodic/abc123" via grep).
    return {
        "uri": f"memory/{class_name}/{entry.id}",
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
