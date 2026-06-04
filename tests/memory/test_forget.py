"""Tests for forget_entry — archive an entry + drop its index rows.

Shared single-source-of-truth behind the `durin memory forget` CLI and
the agent's `memory_forget` tool.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from durin.memory.fts_index import FTSIndex
from durin.memory.forget import ForgetError, forget_entry, parse_memory_uri
from durin.memory.indexer import reindex_one_file
from durin.memory.store import store_memory


def _store(ws: Path, content: str, entities: list[str], class_name: str = "stable") -> str:
    res = store_memory(
        ws,
        content=content,
        class_name=class_name,
        entities=entities,
        valid_from=datetime.date(2026, 6, 4),
    )
    return res["id"]


def _fts_uris(ws: Path) -> set[str]:
    with FTSIndex.open(ws) as idx:
        return {uri for uri, _ in idx.known_uris()}


# ---------------------------------------------------------------------------
# parse_memory_uri
# ---------------------------------------------------------------------------


def test_parse_memory_uri_variants() -> None:
    assert parse_memory_uri("memory/stable/abc") == ("stable", "abc")
    assert parse_memory_uri("./memory/stable/abc.md") == ("stable", "abc")
    with pytest.raises(ForgetError):
        parse_memory_uri("stable/abc")
    with pytest.raises(ForgetError):
        parse_memory_uri("memory/stable")


# ---------------------------------------------------------------------------
# forget_entry happy path
# ---------------------------------------------------------------------------


def test_forget_archives_and_unindexes_fts(tmp_path: Path) -> None:
    entry_id = _store(tmp_path, "mxhero profile", ["company:mxhero"])
    entry_path = tmp_path / "memory" / "stable" / f"{entry_id}.md"
    uri = f"memory/stable/{entry_id}"
    reindex_one_file(tmp_path, entry_path, trigger="test")
    assert uri in _fts_uris(tmp_path)  # precondition

    dest = forget_entry(tmp_path, uri)

    # File moved out of the live class into archive/.
    assert not entry_path.exists()
    assert dest.exists()
    assert dest.parts[-3:] == ("archive", "stable", f"{entry_id}.md")
    # FTS row gone — the entry is no longer searchable.
    assert uri not in _fts_uris(tmp_path)


def test_forget_refuses_entities(tmp_path: Path) -> None:
    with pytest.raises(ForgetError, match="entit"):
        forget_entry(tmp_path, "memory/entities/person:marcelo")


def test_forget_unsupported_class(tmp_path: Path) -> None:
    with pytest.raises(ForgetError):
        forget_entry(tmp_path, "memory/pending/abc")


def test_forget_missing_entry(tmp_path: Path) -> None:
    with pytest.raises(ForgetError, match="not found"):
        forget_entry(tmp_path, "memory/stable/nonexistent")
