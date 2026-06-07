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


def _store_stable(ws: Path, content: str, entities: list[str], day: int = 4) -> None:
    store_memory(
        ws, content=content, class_name="stable", entities=entities,
        valid_from=datetime.date(2026, 6, day),
    )


def test_entity_detail_missing_returns_none(tmp_path: Path) -> None:
    assert get_entity_detail(tmp_path, "person:nobody") is None


def test_phantom_entity_returns_referencing_entries(tmp_path: Path) -> None:
    """An entity tagged in a stable entry but with no consolidated page must
    return a detail (page=None) carrying its referencing entries, instead of
    404ing into an empty side panel."""
    _store_stable(tmp_path, "mxHERO company profile", ["company:mxhero"])
    d = get_entity_detail(tmp_path, "company:mxhero")
    assert d is not None
    assert d["page"] is None
    assert d["ref"] == "company:mxhero"
    classes = {e["class"] for e in d["entries"]}
    assert "stable" in classes
    assert any("mxHERO" in (e.get("body") or "") for e in d["entries"])


def test_unknown_entity_with_no_entries_returns_none(tmp_path: Path) -> None:
    """A ref with neither a page nor any referencing entry/archive is a real
    miss → None (404), not a fabricated empty detail."""
    _store_stable(tmp_path, "mxHERO profile", ["company:mxhero"])
    assert get_entity_detail(tmp_path, "company:ghost") is None


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


# ---------------------------------------------------------------------------
# provenance surfacing — who/when/from-which-session each fact came from
# ---------------------------------------------------------------------------


def _provenanced_page(tmp_path: Path) -> Path:
    """An agent-created topic with a relation whose provenance points back to
    a session turn — mirrors what `memory_upsert_entity` writes."""
    import datetime as _dt

    page = EntityPage(
        type="topic",
        name="Reacción Adversa",
        relations=[{"to": "topic:veterinaria", "type": "related_to"}],
        derived_from=["reference:rabies-investigation"],
        provenance={
            "relations": {
                "topic:veterinaria\x1frelated_to": {
                    "to": "topic:veterinaria", "type": "related_to",
                    "source_ref": "[[sessions/websocket_abc123.md#turn-8]]",
                    "extracted_at": "2026-06-06T18:41:40.401981+00:00",
                    "author": "agent",
                },
            },
            "attributes": {
                "severity": {
                    "source_ref": "[[sessions/cli_direct.md#turn-3]]",
                    "extracted_at": "2026-06-06T19:00:00+00:00",
                    "author": "dream",
                },
            },
            "derived_from": {
                "reference:rabies-investigation": {
                    "source_ref": "[[sessions/websocket_abc123.md#turn-8]]",
                    "extracted_at": "2026-06-06T18:42:00+00:00",
                    "author": "agent",
                },
            },
        },
        author="agent_created",
        created_at=_dt.datetime(2026, 6, 6, 18, 41, 40, tzinfo=_dt.timezone.utc),
    )
    path = tmp_path / "memory" / "entities" / "topic" / "reaccion.md"
    page.save(path)
    return path


def test_serialize_page_exposes_relations_and_author(tmp_path: Path) -> None:
    _provenanced_page(tmp_path)
    d = get_entity_detail(tmp_path, "topic:reaccion")
    assert d is not None
    page = d["page"]
    assert page["author"] == "agent_created"
    assert page["created_at"].startswith("2026-06-06T18:41:40")
    assert {"to": "topic:veterinaria", "type": "related_to"} in page["relations"]
    assert page["derived_from"] == ["reference:rabies-investigation"]


def test_provenance_events_parse_session_and_turn(tmp_path: Path) -> None:
    _provenanced_page(tmp_path)
    d = get_entity_detail(tmp_path, "topic:reaccion")
    assert d is not None
    events = d["provenance"]
    rel = next(e for e in events if e["kind"] == "relation")
    assert rel["author"] == "agent"
    assert rel["session_stem"] == "websocket_abc123"
    assert rel["turn"] == 8
    assert rel["detail"] == "related_to → topic:veterinaria"
    assert rel["when"].startswith("2026-06-06T18:41:40")

    attr = next(e for e in events if e["kind"] == "attribute")
    assert attr["author"] == "dream"
    assert attr["session_stem"] == "cli_direct"
    assert attr["turn"] == 3
    assert attr["detail"] == "severity"

    df = next(e for e in events if e["kind"] == "derived_from")
    assert df["author"] == "agent"
    assert df["detail"] == "reference:rabies-investigation"
    assert df["session_stem"] == "websocket_abc123"
    assert df["turn"] == 8


def test_provenance_non_session_ref_has_no_link(tmp_path: Path) -> None:
    import datetime as _dt

    page = EntityPage(
        type="topic", name="X",
        relations=[{"to": "topic:y", "type": "related_to"}],
        provenance={
            "relations": {
                "topic:y\x1frelated_to": {
                    "to": "topic:y", "type": "related_to",
                    "source_ref": "memory_upsert_entity",  # fallback, not a session
                    "extracted_at": "2026-06-06T18:41:40+00:00",
                    "author": "agent",
                },
            },
        },
        created_at=_dt.datetime(2026, 6, 6, tzinfo=_dt.timezone.utc),
    )
    page.save(tmp_path / "memory" / "entities" / "topic" / "x.md")
    d = get_entity_detail(tmp_path, "topic:x")
    assert d is not None
    rel = next(e for e in d["provenance"] if e["kind"] == "relation")
    assert rel["session_stem"] is None
    assert rel["turn"] is None
    assert rel["source_ref"] == "memory_upsert_entity"


def test_provenance_empty_when_no_provenance(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "plain", name="Plain")
    d = get_entity_detail(tmp_path, "person:plain")
    assert d is not None
    assert d["provenance"] == []


# ---------------------------------------------------------------------------
# session detail — full thread (so the panel can scroll to a provenance moment)
# ---------------------------------------------------------------------------


def _write_session_jsonl(tmp_path: Path, stem: str, n: int) -> None:
    import json as _json

    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    lines: list[dict] = [{"channel": "websocket", "title": "T"}]
    for i in range(n):
        lines.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"msg {i}",
            "timestamp": f"2026-06-06T10:{i:02d}:00",
        })
    (sdir / f"{stem}.jsonl").write_text(
        "\n".join(_json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )


def test_session_detail_returns_full_thread_with_index(tmp_path: Path) -> None:
    from durin.memory.graph_api import get_session_detail

    _write_session_jsonl(tmp_path, "websocket_s", 15)
    d = get_session_detail(tmp_path, "websocket_s")
    assert d is not None
    msgs = d["recent_messages"]
    # Full thread (chronological, with index + ts), not just the last 10 —
    # the panel needs the earlier messages to scroll to an old provenance
    # moment.
    assert len(msgs) == 15
    assert msgs[0]["index"] == 0
    assert msgs[0]["preview"] == "msg 0"
    assert msgs[0]["ts"] == "2026-06-06T10:00:00"
    assert msgs[-1]["index"] == 14
    assert msgs[-1]["preview"] == "msg 14"


def test_entity_detail_referencing_entries_surface(tmp_path: Path) -> None:
    """All entries tagging the entity surface (two-track model, N3: fragments
    are not consolidated, so there is no cursor filter)."""
    _write_page(tmp_path, "person", "m")
    _store(tmp_path, "older obs", ["person:m"], day=1)
    _store(tmp_path, "newer obs", ["person:m"], day=5)
    d = get_entity_detail(tmp_path, "person:m")
    assert d is not None
    bodies = [e["body"] for e in d["entries"]]
    assert any("older obs" in b for b in bodies)
    assert any("newer obs" in b for b in bodies)


def test_entity_detail_includes_archive(tmp_path: Path) -> None:
    """Spec layout (doc memory §3.2): archives live at
    `memory/archive/entities/<type>/<absorbed_slug>.md` and carry
    `archived_into = <type>:<canonical_slug>`.
    """
    _write_page(tmp_path, "person", "marcelo")
    archive_dir = tmp_path / "memory" / "archive" / "entities" / "person"
    archive_dir.mkdir(parents=True)
    EntityPage(
        type="person", name="Old M", aliases=[],
        extra={
            "archived_into": "person:marcelo",
            "archived_at": "2026-05-23T18:00:00+00:00",
            "archived_reason": "auto",
        },
    ).save(archive_dir / "marcelo_old.md")
    d = get_entity_detail(tmp_path, "person:marcelo")
    assert d is not None
    assert len(d["archive"]) == 1
    a = d["archive"][0]
    assert a["slug"] == "marcelo_old"
    assert a["name"] == "Old M"
    assert a["archived_reason"] == "auto"
    assert a["archived_at"] is not None
    assert a["archived_into"] == "person:marcelo"


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


# ---------------------------------------------------------------------------
# session detail
# ---------------------------------------------------------------------------


def _write_session_fixture(
    ws: Path, stem: str, *,
    messages: int = 0,
    title: str | None = None,
    meta: dict | None = None,
) -> None:
    import json
    sd = ws / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    lines: list[dict] = []
    if title:
        lines.append({"title": title, "channel": "websocket", "model": "glm-5.1"})
    for i in range(messages):
        lines.append({"role": "user", "content": f"msg {i}", "ts": 1000 + i})
    (sd / f"{stem}.jsonl").write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8"
    )
    if meta is not None:
        (sd / f"{stem}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_session_detail_missing_returns_none(tmp_path: Path) -> None:
    from durin.memory.graph_api import get_session_detail
    assert get_session_detail(tmp_path, "nobody") is None


def test_session_detail_basic_info(tmp_path: Path) -> None:
    from durin.memory.graph_api import get_session_detail
    _write_session_fixture(tmp_path, "sess1", title="Hello", messages=5)
    d = get_session_detail(tmp_path, "sess1")
    assert d is not None
    assert d["session_ref"] == "session:sess1"
    assert d["info"]["title"] == "Hello"
    assert d["info"]["channel"] == "websocket"
    assert d["info"]["model"] == "glm-5.1"
    assert d["info"]["message_count"] == 5
    assert len(d["recent_messages"]) == 5
    assert d["events"] == []
    assert d["memory_ops"] == []
    assert d["entries_linked"] == []


def test_session_detail_recent_messages_capped(tmp_path: Path) -> None:
    from durin.memory.graph_api import get_session_detail
    _write_session_fixture(tmp_path, "sess1", title="x", messages=25)
    d = get_session_detail(tmp_path, "sess1", recent_messages=10)
    assert d is not None
    assert d["info"]["message_count"] == 25
    assert len(d["recent_messages"]) == 10
    # Tail should be the LAST messages.
    assert d["recent_messages"][-1]["preview"].startswith("msg 24")


def test_session_detail_filters_memory_ops_from_events(tmp_path: Path) -> None:
    from durin.memory.graph_api import get_session_detail
    _write_session_fixture(
        tmp_path, "sess1", messages=2,
        meta={
            "session_key": "websocket:sess1",
            "events": [
                {"type": "tool_call", "tool": "memory_store", "ts": "2026-05-22T00:00:00"},
                {"type": "tool_call", "tool": "read_file", "ts": "2026-05-22T00:01:00"},
                {"type": "tool_call", "tool": "memory_search", "ts": "2026-05-22T00:02:00"},
                {"type": "plan", "title": "x"},
            ],
            "derived": {},
        },
    )
    d = get_session_detail(tmp_path, "sess1")
    assert d is not None
    assert len(d["events"]) == 4
    # Only memory_* tools surface in memory_ops; plan is excluded.
    tools = [op["tool"] for op in d["memory_ops"]]
    assert tools == ["memory_store", "memory_search"]


def test_session_detail_finds_entries_linked_via_source_refs(tmp_path: Path) -> None:
    from durin.memory.graph_api import get_session_detail
    _write_session_fixture(tmp_path, "sess1", messages=1)
    # Two entries: one linked, one not.
    store_memory(
        tmp_path, content="linked",
        entities=["person:m"],
        source_refs=["sessions/sess1.md#turn-2"],
        valid_from=datetime.date(2026, 5, 22),
    )
    store_memory(
        tmp_path, content="unlinked",
        entities=["person:m"],
        valid_from=datetime.date(2026, 5, 22),
    )
    d = get_session_detail(tmp_path, "sess1")
    assert d is not None
    assert len(d["entries_linked"]) == 1
    assert d["entries_linked"][0]["snippet"].startswith("linked")
    # entities_tagged.from_source_refs aggregates the linked entries' entities.
    assert d["entities_tagged"]["from_source_refs"] == ["person:m"]


def test_session_detail_meta_tags_separate_from_source_refs(tmp_path: Path) -> None:
    from durin.memory.graph_api import get_session_detail
    _write_session_fixture(
        tmp_path, "sess1", messages=1,
        meta={
            "session_key": "websocket:sess1",
            "events": [],
            "derived": {
                "_last_tags": {"entities": ["topic:autocompact"]},
            },
        },
    )
    d = get_session_detail(tmp_path, "sess1")
    assert d is not None
    assert d["entities_tagged"]["from_meta"] == ["topic:autocompact"]
    assert d["entities_tagged"]["from_source_refs"] == []
