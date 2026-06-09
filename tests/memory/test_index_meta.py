"""Index meta.json helper.

Per `docs/architecture/memory/02_indexing.md` §2 + §7.2:

- Lives at `<workspace>/.durin/index/meta.json`.
- Carries `schema_version` (int) and `embedding_model_id` (str).
- Missing → fresh install (caller treats as empty).
- Read returns a frozen dataclass-ish record.
- Write is atomic (tmp + rename).

Phase 0 scope: the **field is present** (read + write + load returns
None when missing). The §7.2 "refuse on mismatch" enforcement consumer
is a later phase.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.memory.index_meta import (
    CURRENT_SCHEMA_VERSION,
    IndexMeta,
    load_index_meta,
    meta_path,
    save_index_meta,
)


def test_meta_path_is_under_durin_index(tmp_path: Path) -> None:
    path = meta_path(tmp_path)
    assert path == tmp_path / ".durin" / "index" / "meta.json"


def test_load_missing_returns_none(tmp_path: Path) -> None:
    """Fresh install: meta.json is absent — return None, do not raise."""
    assert load_index_meta(tmp_path) is None


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    meta = IndexMeta(
        schema_version=CURRENT_SCHEMA_VERSION,
        embedding_model_id="paraphrase-multilingual-MiniLM-L12-v2",
        last_full_rebuild="2026-05-28T00:00:00Z",
        previous_models=(),
    )
    save_index_meta(tmp_path, meta)
    loaded = load_index_meta(tmp_path)
    assert loaded == meta


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    """Caller doesn't have to mkdir `.durin/index/` first."""
    assert not (tmp_path / ".durin").exists()
    meta = IndexMeta(
        schema_version=1,
        embedding_model_id="m",
        last_full_rebuild=None,
        previous_models=(),
    )
    save_index_meta(tmp_path, meta)
    assert meta_path(tmp_path).exists()


def test_save_is_atomic(tmp_path: Path) -> None:
    """A failed save must not leave a half-written file. Implementation
    detail: writes go through a `.tmp` sibling + rename. Tested by
    checking that after a successful save there's no leftover `.tmp`."""
    save_index_meta(
        tmp_path,
        IndexMeta(
            schema_version=1,
            embedding_model_id="m",
            last_full_rebuild=None,
            previous_models=(),
        ),
    )
    tmp_siblings = list((tmp_path / ".durin" / "index").glob("meta.json.*"))
    assert tmp_siblings == []


def test_loaded_json_is_well_formed(tmp_path: Path) -> None:
    """The on-disk shape should be valid JSON with the documented keys."""
    save_index_meta(
        tmp_path,
        IndexMeta(
            schema_version=CURRENT_SCHEMA_VERSION,
            embedding_model_id="m",
            last_full_rebuild="2026-01-01T00:00:00Z",
            previous_models=("old-model-v1",),
        ),
    )
    raw = json.loads(meta_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["schema_version"] == CURRENT_SCHEMA_VERSION
    assert raw["embedding_model_id"] == "m"
    assert raw["last_full_rebuild"] == "2026-01-01T00:00:00Z"
    assert raw["previous_models"] == ["old-model-v1"]


def test_load_malformed_returns_none(tmp_path: Path) -> None:
    """Corrupted file → treat as missing. The caller's normal fresh-
    install path then runs a rebuild. Better than crashing on startup."""
    path = meta_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert load_index_meta(tmp_path) is None


def test_load_missing_fields_returns_none(tmp_path: Path) -> None:
    """Field names changed at some point → reject gracefully."""
    path = meta_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    # Missing `embedding_model_id` is fatal: reject.
    assert load_index_meta(tmp_path) is None


def test_current_schema_version_is_positive_int() -> None:
    assert isinstance(CURRENT_SCHEMA_VERSION, int)
    assert CURRENT_SCHEMA_VERSION >= 1


def test_schema_version_is_6_for_session_fts() -> None:
    """v6 forces a rebuild so existing workspaces pick up the raw
    session turn rows (one FTS row per ``## turn-N``, type "session" —
    indexer third pass). v5 did the same for skill rows."""
    assert CURRENT_SCHEMA_VERSION == 6
