"""Tests for P12 read/forget/backlinks helpers in ``graph_api``."""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from durin.memory.aliases_cache import _clear_all
from durin.memory.graph_api import (
    forget_entry,
    get_entry_backlinks,
    get_entry_detail,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    _clear_all()
    yield
    _clear_all()


@pytest.fixture(autouse=True)
def _disable_memory_cfg():
    """forget_entry tries to read load_config() for vector cleanup. Patch
    it to a memory-disabled config so the cleanup path is a no-op and
    we don't need fastembed available in the test environment."""
    fake_cfg = SimpleNamespace(
        memory=SimpleNamespace(
            enabled=False,
            embedding=SimpleNamespace(model=""),
        ),
    )
    with patch("durin.config.loader.load_config", return_value=fake_cfg):
        yield


def _seed_entry(
    ws: Path,
    *,
    class_name: str = "episodic",
    entry_id: str,
    body: str = "obs",
    entities: tuple[str, ...] = ("person:alice",),
    source_refs: tuple[str, ...] = (),
) -> Path:
    """Write a memory entry directly with a chosen id (bypasses
    store_memory's content-hash id derivation so tests can use
    stable URIs in assertions)."""
    ent_lines = (
        "entities:\n" + "".join(f"  - {e}\n" for e in entities)
        if entities else ""
    )
    src_lines = (
        "source_refs:\n" + "".join(f"  - {s}\n" for s in source_refs)
        if source_refs else ""
    )
    fm = (
        f"id: {entry_id}\n"
        f"headline: {entry_id} headline\n"
        f"valid_from: 2026-05-30\n"
        f"{ent_lines}"
        f"{src_lines}"
    )
    p = ws / "memory" / class_name / f"{entry_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return p


def _seed_episodic(ws: Path, entry_id: str, **kw) -> Path:
    return _seed_entry(ws, class_name="episodic", entry_id=entry_id, **kw)


# ---------------------------------------------------------------------------
# get_entry_detail
# ---------------------------------------------------------------------------


def test_get_entry_detail_happy_path(tmp_path: Path) -> None:
    _seed_episodic(tmp_path, "obs-1", body="Alice loves rust", entities=("person:alice",))
    detail = get_entry_detail(tmp_path, "memory/episodic/obs-1")
    assert detail is not None
    assert detail["uri"] == "memory/episodic/obs-1"
    assert detail["class_name"] == "episodic"
    assert detail["exists"] is True
    assert detail["frontmatter"]["entities"] == ["person:alice"]
    assert detail["frontmatter"]["headline"]  # auto-generated
    assert "Alice loves rust" in detail["body"]


def test_get_entry_detail_returns_none_when_missing(tmp_path: Path) -> None:
    assert get_entry_detail(tmp_path, "memory/episodic/ghost") is None


def test_get_entry_detail_returns_none_for_bad_uri(tmp_path: Path) -> None:
    assert get_entry_detail(tmp_path, "not-a-uri") is None
    assert get_entry_detail(tmp_path, "") is None
    assert get_entry_detail(tmp_path, "memory/episodic") is None
    assert get_entry_detail(tmp_path, "memory/entities/person/marcelo") is None


def test_get_entry_detail_tolerates_dot_md_suffix(tmp_path: Path) -> None:
    _seed_episodic(tmp_path, "obs-2")
    detail = get_entry_detail(tmp_path, "memory/episodic/obs-2.md")
    assert detail is not None
    assert detail["uri"] == "memory/episodic/obs-2"


# ---------------------------------------------------------------------------
# forget_entry
# ---------------------------------------------------------------------------


def test_forget_entry_archives_episodic(tmp_path: Path) -> None:
    src = _seed_episodic(tmp_path, "obs-1")
    result = forget_entry(tmp_path, "memory/episodic/obs-1")
    assert result == {"result": "archived"}
    assert not src.exists()
    archived = tmp_path / "memory" / "archive" / "episodic" / "obs-1.md"
    assert archived.exists()


def test_forget_entry_returns_not_found(tmp_path: Path) -> None:
    assert forget_entry(tmp_path, "memory/episodic/ghost") == {"result": "not_found"}


def test_forget_entry_protected_on_entities_path(tmp_path: Path) -> None:
    """Any URI under memory/entities/... must return 'protected'."""
    result = forget_entry(tmp_path, "memory/entities/person/marcelo")
    assert result == {"result": "protected"}


def test_forget_entry_invalid_uri(tmp_path: Path) -> None:
    assert forget_entry(tmp_path, "")["result"] == "invalid"
    assert forget_entry(tmp_path, "garbage")["result"] == "invalid"


def test_forget_entry_unsupported_class(tmp_path: Path) -> None:
    assert forget_entry(tmp_path, "memory/garbage/x")["result"] == "invalid"


# ---------------------------------------------------------------------------
# get_entry_backlinks
# ---------------------------------------------------------------------------


def test_backlinks_finds_wikilink_in_body(tmp_path: Path) -> None:
    _seed_episodic(tmp_path, "target-1", body="target body")
    _seed_episodic(
        tmp_path, "referrer-1",
        body="see [[memory/episodic/target-1]] for context",
        entities=("person:bob",),
    )
    out = get_entry_backlinks(tmp_path, "memory/episodic/target-1")
    assert out["uri"] == "memory/episodic/target-1"
    assert len(out["backlinks"]) == 1
    assert out["backlinks"][0]["uri"] == "memory/episodic/referrer-1"
    assert "body" in out["backlinks"][0]["context"]
    assert out["truncated"] is False


def test_backlinks_finds_source_refs(tmp_path: Path) -> None:
    _seed_episodic(tmp_path, "target-1")
    _seed_episodic(
        tmp_path, "referrer-2",
        body="downstream obs",
        entities=("person:bob",),
        source_refs=("memory/episodic/target-1",),
    )
    out = get_entry_backlinks(tmp_path, "memory/episodic/target-1")
    assert len(out["backlinks"]) == 1
    assert "source_refs" in out["backlinks"][0]["context"]


def test_backlinks_excludes_self(tmp_path: Path) -> None:
    """An entry that mentions its own URI in its body must not be its
    own backlink (UI confusion otherwise)."""
    _seed_episodic(
        tmp_path, "me",
        body="this body mentions [[memory/episodic/me]] (the same entry)",
        entities=("person:bob",),
    )
    out = get_entry_backlinks(tmp_path, "memory/episodic/me")
    assert out["backlinks"] == []


def test_backlinks_returns_empty_when_no_references(tmp_path: Path) -> None:
    _seed_episodic(tmp_path, "lonely")
    _seed_episodic(tmp_path, "also-lonely", body="unrelated")
    out = get_entry_backlinks(tmp_path, "memory/episodic/lonely")
    assert out["backlinks"] == []


def test_backlinks_truncates_to_limit(tmp_path: Path) -> None:
    _seed_episodic(tmp_path, "popular")
    for i in range(5):
        _seed_episodic(
            tmp_path, f"ref-{i}",
            body=f"see [[memory/episodic/popular]] #{i}",
            entities=("person:bob",),
        )
    out = get_entry_backlinks(tmp_path, "memory/episodic/popular", limit=3)
    assert len(out["backlinks"]) == 3
    assert out["truncated"] is True
