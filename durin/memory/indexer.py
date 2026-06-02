"""Workspace ↔ FTS5 indexer.

Per `docs/architecture/memory/02_indexing.md` §6: the indexer is the bridge between
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
from typing import Any, Optional

from durin.memory.entity_page import EntityPage
from durin.memory.fts_index import FTSIndex
from durin.memory.paths import (
    skill_path_from_uri,
    skill_uri,
    skills_dir,
    walk_memory,
    walk_skills,
)
from durin.memory.storage import load_entry

__all__ = [
    "IndexStats",
    "detect_index_staleness",
    "ensure_index_fresh",
    "rebuild_fts_index",
    "reindex_one_file",
    "reindex_one_skill",
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
    if not ((workspace / "memory").is_dir() or skills_dir(workspace).is_dir()):
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
        # Second pass: index skills/<name>/SKILL.md alongside memory files.
        from durin.memory.paths import walk_skills

        for skill_md in walk_skills(workspace):
            try:
                payload = _payload_for_skill(workspace, skill_md)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "indexer: skipping %s: %s", skill_md, exc,
                )
                errors += 1
                continue
            if payload is None:
                continue
            try:
                idx.upsert(**payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "indexer: upsert %s failed: %s", skill_md, exc,
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


def _emit_write(
    *,
    uri: str,
    op: str,
    index: str = "fts",
    trigger: str = "watcher",
    duration_ms: float = 0.0,
) -> None:
    """E5 (audit second pass, 2026-05-28): added `trigger` +
    `duration_ms` so the documented dashboards become computable
    (doc 07 §10.3 `index_write_p95_ms` < 50ms; doc 09 §216 trigram
    capacity monitoring needs to distinguish watcher vs drift_repair
    bursts).
    """
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(
            "memory.index.write",
            {
                "uri": uri,
                "op": op,
                "index": index,
                "trigger": trigger,
                "duration_ms": duration_ms,
            },
        )
    except Exception:  # pragma: no cover
        pass


def _emit_staleness(
    *,
    uri: str,
    reason: str,
    delta_seconds: float | None = None,
) -> None:
    """Used by the health-check cron + the indexer's drift detection.

    Audit G3 (2026-05-28): when ``reason='mtime_lag'`` the caller
    supplies ``delta_seconds = current_file_mtime - indexed_mtime``
    so dashboards can graph p50/p95 staleness magnitude (not just
    event count). For ``missing_row`` and ``row_for_missing_file``
    there's no indexed_mtime to compare against, so the field is
    omitted — kept as ``NotRequired[float]`` in the TypedDict.
    """
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        payload: dict[str, Any] = {"uri": uri, "reason": reason}
        if delta_seconds is not None:
            payload["delta_seconds"] = float(delta_seconds)
        emit_tool_event(
            "memory.index.staleness_detected", payload,
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
    # Skills live under skills/<name>/SKILL.md (outside memory/), so
    # walk_memory never yields them. Without this pass every indexed
    # skill row looks like a `row_for_missing_file` and drift repair
    # SILENTLY DELETES it. Compute the uri exactly as _payload_for_skill
    # does (slug = skill_md.parent.name) so the fs uri matches the
    # indexed uri and the row is recognized as backed by a real file.
    for skill_md in walk_skills(workspace):
        uri = skill_uri(skill_md.parent.name)
        fs_files[uri] = skill_md.stat().st_mtime

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
                # G3 (audit fourth pass, 2026-05-28): include the
                # magnitude of the gap so dashboards can graph
                # staleness p50/p95 instead of just event counts.
                delta = current - indexed_mtime
                _emit_staleness(
                    uri=uri,
                    reason="mtime_lag",
                    delta_seconds=delta,
                )
                issues.append({
                    "uri": uri,
                    "reason": "mtime_lag",
                    "delta_seconds": delta,
                })

    for uri in fs_files:
        if uri not in seen_in_index:
            _emit_staleness(uri=uri, reason="missing_row")
            issues.append({"uri": uri, "reason": "missing_row"})

    return issues


def reindex_one_file(
    workspace: Path,
    md_path: Path,
    *,
    trigger: str = "watcher",
) -> None:
    """Synchronous re-index for one file. Called after every write.

    No-ops when:
    - The path is not under ``<workspace>/memory/``.
    - The path is under ``memory/archive/`` or ``memory/pending/``.
    - The file disappeared between the write and this call (we
      delete the row instead).

    E5 (audit second pass, 2026-05-28): ``trigger`` propagates the
    caller context into ``memory.index.write`` so dashboards can
    split steady-state (`watcher`) from burst (`dream_apply`,
    `drift_repair`) writes. Default is `watcher` because that's the
    most common callsite (every agent write goes through the file
    watcher).
    """
    import time

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
                t0 = time.monotonic()
                idx.delete_by_uri(uri)
                duration_ms = (time.monotonic() - t0) * 1000.0
                _emit_write(
                    uri=uri, op="delete",
                    trigger=trigger, duration_ms=duration_ms,
                )
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
            t0 = time.monotonic()
            idx.upsert(**payload)
            duration_ms = (time.monotonic() - t0) * 1000.0
            _emit_write(
                uri=payload["uri"], op="upsert",
                trigger=trigger, duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "indexer: incremental upsert %s failed: %s",
                md_path, exc,
            )


def reindex_one_skill(
    workspace: Path,
    skill_md: Path,
    *,
    trigger: str = "skill_store",
) -> None:
    """Synchronous re-index for one ``skills/<name>/SKILL.md``.

    Mirrors :func:`reindex_one_file` for the skills tree: called by the
    skills_store after a skill is created/edited/deleted. If the file
    disappeared (skill deleted) the row is evicted instead.

    The default ``trigger`` is ``skill_store`` because every callsite is
    the git-backed skills authoring path; it propagates into
    ``memory.index.write`` so dashboards can split skill writes from
    memory writes.
    """
    import time

    workspace = Path(workspace)
    skill_md = Path(skill_md)
    with FTSIndex.open(workspace) as idx:
        if not skill_md.is_file():
            uri = skill_uri(skill_md.parent.name)
            t0 = time.monotonic()
            idx.delete_by_uri(uri)
            duration_ms = (time.monotonic() - t0) * 1000.0
            _emit_write(
                uri=uri, op="delete",
                trigger=trigger, duration_ms=duration_ms,
            )
            return
        try:
            payload = _payload_for_skill(workspace, skill_md)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "indexer: skip incremental skill %s: %s", skill_md, exc,
            )
            return
        if payload is None:
            return
        try:
            t0 = time.monotonic()
            idx.upsert(**payload)
            duration_ms = (time.monotonic() - t0) * 1000.0
            _emit_write(
                uri=payload["uri"], op="upsert",
                trigger=trigger, duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "indexer: incremental skill upsert %s failed: %s",
                skill_md, exc,
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


def _skill_text(sp) -> str:  # type: ignore[no-untyped-def]
    """Compose the BM25 text for one skill page (name + description +
    full body, mirroring the entity-page composition)."""
    return f"{sp.name}\n{sp.description}\n{sp.body}"


def _payload_for_skill(workspace: Path, skill_md: Path) -> Optional[dict]:
    """Derive the upsert payload for one ``skills/<name>/SKILL.md``.

    Returns ``None`` when the file is unreadable/malformed (SkillPage
    returns ``None``). The ``uri`` is ``skill/<slug>`` where ``<slug>``
    is the skill directory name — identical to :func:`_uri_for`'s skill
    branch so the rebuild and incremental paths dedup cleanly.
    """
    from durin.memory.skill_page import SkillPage

    sp = SkillPage.from_file(skill_md)
    if sp is None:
        return None
    slug = skill_md.parent.name
    uri = skill_uri(slug)
    return {
        "uri": uri,
        "path": skill_path_from_uri(uri),
        "type_": "skill",
        "entity_type": None,
        "text": _skill_text(sp),
        "mtime": skill_md.stat().st_mtime,
    }


def _uri_for(workspace: Path, md_path: Path) -> Optional[str]:
    """Best-effort URI derivation for a deleted file (no file content
    available). For skill pages the URI is ``skill/<slug>``; for entity
    pages it's ``<type>:<slug>``; for entries it's the bare ``id`` which
    equals the filename stem."""
    # Skills live under <workspace>/skills/, outside memory/. Check this
    # branch first — the memory/ relative_to below raises for skill paths.
    try:
        skill_parts = md_path.relative_to(skills_dir(workspace)).parts
    except ValueError:
        skill_parts = ()
    if skill_parts:
        return skill_uri(skill_parts[0])
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
