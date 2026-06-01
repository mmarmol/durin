"""Auto-rebuild when `meta.json::schema_version` differs from code (P2.2).

Per `docs/architecture/memory/02_indexing.md` §7.2 + doc 10 P2.2: the indexer must
detect a stale schema on startup and trigger a rebuild. The hook
lives in :func:`durin.memory.indexer.ensure_index_fresh`. Idempotent
per workspace within a single process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.fts_index import FTSIndex
from durin.memory.index_meta import (
    CURRENT_SCHEMA_VERSION,
    IndexMeta,
    load_index_meta,
    save_index_meta,
)


def _seed_one_entity(workspace: Path) -> None:
    page = EntityPage(
        type="person", name="Marcelo", aliases=["m"],
        body="content for marcelo",
    )
    page.save(workspace / "memory" / "entities" / "person" / "marcelo.md")


def test_missing_meta_triggers_rebuild(tmp_path: Path) -> None:
    """Fresh workspace: no `meta.json` → rebuild runs + meta saved."""
    from durin.memory.indexer import ensure_index_fresh
    _seed_one_entity(tmp_path)
    assert load_index_meta(tmp_path) is None

    result = ensure_index_fresh(tmp_path)
    assert result["rebuilt"] is True
    assert result["reason"] in ("missing_meta", "schema_mismatch")
    # Meta now exists at the canonical version.
    meta = load_index_meta(tmp_path)
    assert meta is not None
    assert meta.schema_version == CURRENT_SCHEMA_VERSION
    # FTS reflects the seeded entity.
    with FTSIndex.open(tmp_path) as idx:
        assert idx.count() >= 1


def test_stale_schema_triggers_rebuild(tmp_path: Path) -> None:
    """Existing meta with old schema_version → rebuild + bump."""
    from durin.memory.indexer import ensure_index_fresh
    _seed_one_entity(tmp_path)
    save_index_meta(tmp_path, IndexMeta(
        schema_version=1,  # stale
        embedding_model_id="m",
    ))
    result = ensure_index_fresh(tmp_path)
    assert result["rebuilt"] is True
    assert result["reason"] == "schema_mismatch"
    meta = load_index_meta(tmp_path)
    assert meta.schema_version == CURRENT_SCHEMA_VERSION


def test_current_schema_skips_rebuild(tmp_path: Path) -> None:
    """Meta already current → no rebuild."""
    from durin.memory.indexer import ensure_index_fresh
    _seed_one_entity(tmp_path)
    save_index_meta(tmp_path, IndexMeta(
        schema_version=CURRENT_SCHEMA_VERSION,
        embedding_model_id="m",
    ))
    result = ensure_index_fresh(tmp_path)
    assert result["rebuilt"] is False


def test_idempotent_within_process(tmp_path: Path) -> None:
    """Second call in the same process is a no-op even if we'd
    otherwise rebuild — avoids hammering disk when many tool
    instances are created per session."""
    from durin.memory.indexer import (
        _RESET_FRESHNESS_CACHE_FOR_TESTS,
        ensure_index_fresh,
    )
    _RESET_FRESHNESS_CACHE_FOR_TESTS()
    _seed_one_entity(tmp_path)
    first = ensure_index_fresh(tmp_path)
    second = ensure_index_fresh(tmp_path)
    assert first["rebuilt"] is True
    assert second["rebuilt"] is False
    assert second["reason"] == "cached"


def test_different_workspaces_isolated(tmp_path: Path) -> None:
    """Cache key is the workspace path — two workspaces don't share
    state."""
    from durin.memory.indexer import (
        _RESET_FRESHNESS_CACHE_FOR_TESTS,
        ensure_index_fresh,
    )
    _RESET_FRESHNESS_CACHE_FOR_TESTS()
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    _seed_one_entity(ws_a)
    _seed_one_entity(ws_b)

    ra = ensure_index_fresh(ws_a)
    rb = ensure_index_fresh(ws_b)
    assert ra["rebuilt"] is True
    assert rb["rebuilt"] is True  # separate workspace, fresh check


def test_emits_rebuild_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema-mismatch rebuild emits `memory.index.rebuild` with
    `reason='schema_mismatch'` so dashboards can see it."""
    from durin.memory.indexer import (
        _RESET_FRESHNESS_CACHE_FOR_TESTS,
        ensure_index_fresh,
    )
    _RESET_FRESHNESS_CACHE_FOR_TESTS()
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.indexer._emit_rebuild",
        lambda **kw: events.append(("memory.index.rebuild", kw)),
    )
    _seed_one_entity(tmp_path)
    save_index_meta(tmp_path, IndexMeta(
        schema_version=1, embedding_model_id="m",
    ))
    ensure_index_fresh(tmp_path)
    assert any(e[0] == "memory.index.rebuild" for e in events)


def test_missing_memory_dir_no_op(tmp_path: Path) -> None:
    """If `memory/` doesn't exist (fresh workspace), no rebuild and
    no error. The first `memory_store` later will create it."""
    from durin.memory.indexer import (
        _RESET_FRESHNESS_CACHE_FOR_TESTS,
        ensure_index_fresh,
    )
    _RESET_FRESHNESS_CACHE_FOR_TESTS()
    result = ensure_index_fresh(tmp_path)
    assert result["rebuilt"] is False
    assert result["reason"] in ("no_memory_dir", "cached")
