"""Tests for the read-only memory surfaces consumed by the webui graph view."""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import pytest

from durin.memory.aliases_cache import _clear_all
from durin.memory.entity_page import EntityPage
from durin.memory.graph_api import (
    get_edge_detail,
    get_entity_detail,
    search_memory_api,
)
from durin.memory.store import store_memory


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    _clear_all()
    yield
    _clear_all()


def _write_page(ws: Path, type_: str, slug: str, **kwargs) -> Path:
    page = EntityPage(
        type=type_,
        name=kwargs.pop("name", slug.title()),
        aliases=kwargs.pop("aliases", []),
        body=kwargs.pop("body", ""),
        dream_processed_through=kwargs.pop("dream_processed_through", None),
        extra=kwargs.pop("extra", {}),
    )
    path = ws / "memory" / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


def _store(ws: Path, content: str, entities: list[str], day: int = 1) -> None:
    store_memory(
        ws, content=content, entities=entities,
        valid_from=datetime.date(2026, 5, day),
    )


# ---------------------------------------------------------------------------
# entity detail
# ---------------------------------------------------------------------------


def test_entity_detail_missing_returns_none(tmp_path: Path) -> None:
    assert get_entity_detail(tmp_path, "person:nobody") is None


def test_entity_detail_minimal_page(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo",
                name="Marcelo Marmol", aliases=["Marcelo"],
                body="## Current\nOwner.\n")
    d = get_entity_detail(tmp_path, "person:marcelo")
    assert d is not None
    assert d["ref"] == "person:marcelo"
    assert d["page"]["name"] == "Marcelo Marmol"
    assert d["page"]["aliases"] == ["Marcelo"]
    assert "Owner" in d["page"]["body"]
    assert d["history"] == []
    assert d["archive"] == []
    assert d["entries"] == []


def test_entity_detail_identifiers_promoted(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "m",
                extra={"identifiers": {"email": ["m@x.com"], "github": "marc"}})
    d = get_entity_detail(tmp_path, "person:m")
    assert d is not None
    assert d["page"]["identifiers"] == {"email": ["m@x.com"], "github": "marc"}
    # `identifiers` removed from `extra` after promotion.
    assert "identifiers" not in d["page"]["extra"]


def test_entity_detail_post_cursor_entries_filter(tmp_path: Path) -> None:
    """Entries newer than the cursor surface; pre-cursor ones don't."""
    _write_page(tmp_path, "person", "m",
                dream_processed_through="2026-05-02T00:00:00")
    _store(tmp_path, "pre", ["person:m"], day=1)   # before cursor → hidden
    _store(tmp_path, "post", ["person:m"], day=5)  # after cursor → shown
    d = get_entity_detail(tmp_path, "person:m")
    assert d is not None
    ids = [e["body"][:5] for e in d["entries"]]
    assert any("post" in s for s in ids)
    assert not any("pre" == s.strip() for s in ids)


def test_entity_detail_includes_archive(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    archive_dir = (
        tmp_path / "memory" / "entities" / "person" / "marcelo" / "archive"
    )
    archive_dir.mkdir(parents=True)
    EntityPage(
        type="person", name="Old M", aliases=[],
        extra={
            "absorbed_into": "../../marcelo.md",
            "absorbed_at": "2026-05-23T18:00:00+00:00",
            "absorbed_reason": "auto",
        },
    ).save(archive_dir / "marcelo_old.md")
    d = get_entity_detail(tmp_path, "person:marcelo")
    assert d is not None
    assert len(d["archive"]) == 1
    a = d["archive"][0]
    assert a["slug"] == "marcelo_old"
    assert a["name"] == "Old M"
    assert a["absorbed_reason"] == "auto"
    assert a["absorbed_at"] is not None


def test_entity_detail_bad_ref_returns_none(tmp_path: Path) -> None:
    assert get_entity_detail(tmp_path, "no-colon") is None
    assert get_entity_detail(tmp_path, "") is None


# ---------------------------------------------------------------------------
# search_memory_api
# ---------------------------------------------------------------------------


def test_search_empty_query_returns_noop(tmp_path: Path) -> None:
    payload = asyncio.run(search_memory_api(tmp_path, ""))
    assert payload["results"] == []
    assert payload["strategy"] == "noop"


def test_search_grep_path_finds_entry(tmp_path: Path) -> None:
    _store(tmp_path, "marcelo prefers pytest", ["person:marcelo"])
    payload = asyncio.run(search_memory_api(tmp_path, "pytest"))
    assert payload["total"] >= 1
    found = any("pytest" in (r.get("snippet") or "") for r in payload["results"])
    assert found


def test_search_results_carry_kind(tmp_path: Path) -> None:
    """Doc 25 §2.H: every result must carry the `kind` marker."""
    _store(tmp_path, "marcelo solo", ["person:marcelo"])
    payload = asyncio.run(search_memory_api(tmp_path, "marcelo"))
    for r in payload["results"]:
        assert "kind" in r
        assert r["kind"] in {"canonical", "fragment", "session", "ingested"}


# ---------------------------------------------------------------------------
# edge detail
# ---------------------------------------------------------------------------


def test_edge_detail_empty_when_no_cooccurrence(tmp_path: Path) -> None:
    _store(tmp_path, "marcelo", ["person:marcelo"])
    _store(tmp_path, "durin", ["project:durin"])
    d = get_edge_detail(tmp_path, "person:marcelo", "project:durin")
    assert d["total"] == 0
    assert d["entries"] == []


def test_edge_detail_returns_co_mentioning_entries(tmp_path: Path) -> None:
    _store(tmp_path, "marcelo + durin one", ["person:marcelo", "project:durin"], day=1)
    _store(tmp_path, "marcelo + durin two", ["person:marcelo", "project:durin"], day=2)
    _store(tmp_path, "only marcelo", ["person:marcelo"], day=3)
    d = get_edge_detail(tmp_path, "person:marcelo", "project:durin")
    assert d["total"] == 2
    assert all("durin" in e["snippet"] for e in d["entries"])
    # Sorted newest-first.
    assert d["entries"][0]["valid_from"] >= d["entries"][1]["valid_from"]


def test_edge_detail_respects_limit(tmp_path: Path) -> None:
    for i in range(10):
        _store(tmp_path, f"obs {i}", ["person:a", "person:b"], day=i + 1)
    d = get_edge_detail(tmp_path, "person:a", "person:b", limit=3)
    assert d["total"] == 10  # total is the unbounded count
    assert len(d["entries"]) == 3
