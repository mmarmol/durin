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
    get_reference_detail,
    list_reference_documents,
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
    """Archives live at `memory/archive/entities/<type>/<absorbed_slug>.md`
    and carry `archived_into = <type>:<canonical_slug>`.
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
    """Every search result must carry the `kind` marker."""
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


# ---------------------------------------------------------------------------
# reference documents — the Library shelf (list + detail)
# ---------------------------------------------------------------------------


def _seed_reference_with_outline_and_entity(tmp_path: Path) -> str:
    """Ingest a doc, write an outline sidecar + a derived entity; return slug."""
    import json

    from durin.memory.distill_dream import outline_path_for
    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity
    from durin.memory.reference import ingest_reference

    r = ingest_reference(
        tmp_path,
        "The Durin Handbook",
        "# Intro\n\nDurin is a local agent.\n\n## Setup\n\nRun the gateway.\n",
        source="https://example.com/handbook.pdf",
    )
    outline_path_for(tmp_path, "the-durin-handbook").write_text(
        json.dumps({
            "ref": r.ref, "title": "The Durin Handbook", "chunk_count": r.chunk_count,
            "abstract": "A handbook about the durin local agent.",
            "sections": [
                {"breadcrumb": "Intro", "summary": "What durin is.", "chunk_indices": [0]},
                {"breadcrumb": "Intro › Setup", "summary": "How to run it.",
                 "chunk_indices": [1]},
            ],
        })
    )
    now = datetime.datetime(2026, 6, 5, tzinfo=datetime.timezone.utc)
    src = "[[references/the-durin-handbook.md]]"
    write_entity(
        tmp_path, "project:durin",
        [
            FieldPatch(kind="body_replace",
                       value="The local-first agent the handbook documents.",
                       author="dream", source_ref=src, at=now),
            FieldPatch(kind="derived_from", value=r.ref,
                       author="dream", source_ref=src, at=now),
        ],
        create=True, name="durin",
    )
    return "the-durin-handbook"


def test_list_reference_documents_empty(tmp_path: Path) -> None:
    assert list_reference_documents(tmp_path) == []


def test_list_reference_documents_newest_first(tmp_path: Path) -> None:
    from durin.memory.reference import ingest_reference

    ingest_reference(tmp_path, "Older", "# A\n\nbody.\n")
    # Force a later ingested_at on the second by editing its frontmatter.
    ingest_reference(tmp_path, "Newer", "# B\n\nbody.\n")
    rows = list_reference_documents(tmp_path)
    assert {r["title"] for r in rows} == {"Older", "Newer"}
    # Sorted by ingested_at descending — both are ISO strings, newest first.
    assert rows == sorted(rows, key=lambda r: r["ingested_at"], reverse=True)
    for r in rows:
        assert r["chunk_count"] >= 1
        assert r["distilled"] is False  # no outline written


def test_list_reference_documents_flags_distilled(tmp_path: Path) -> None:
    slug = _seed_reference_with_outline_and_entity(tmp_path)
    rows = list_reference_documents(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == slug
    assert row["ref"] == f"reference:{slug}"
    assert row["source"] == "https://example.com/handbook.pdf"
    assert row["distilled"] is True


def test_get_reference_detail_missing_returns_none(tmp_path: Path) -> None:
    assert get_reference_detail(tmp_path, "ghost") is None


def test_get_reference_detail_full_payload(tmp_path: Path) -> None:
    slug = _seed_reference_with_outline_and_entity(tmp_path)
    d = get_reference_detail(tmp_path, slug)
    assert d is not None
    assert d["title"] == "The Durin Handbook"
    assert d["source"] == "https://example.com/handbook.pdf"
    assert d["chunks_total"] == 2
    # outline (on-disk list form with chunk_indices)
    assert d["outline"]["abstract"].startswith("A handbook")
    crumbs = [s["breadcrumb"] for s in d["outline"]["sections"]]
    assert crumbs == ["Intro", "Intro › Setup"]
    # derived entity surfaces with its cleaned significance; dream-authored
    # derived_from → relation="distilled" (the document is about it).
    assert d["entities"] == [{
        "ref": "project:durin", "type": "project", "name": "durin",
        "relation": "distilled",
        "significance": "The local-first agent the handbook documents.",
    }]
    # chunk preview carries breadcrumb + text
    assert d["chunks_preview"][0]["breadcrumb"] == "Intro"
    assert "Durin is a local agent." in d["chunks_preview"][0]["text"]


def test_get_reference_detail_distinguishes_referenced_from_distilled(
    tmp_path: Path,
) -> None:
    """An agent-authored derived_from (an entity that CITED the doc as a source)
    is relation='referenced', not 'distilled' — the doc isn't about it."""
    from datetime import timezone

    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity
    from durin.memory.reference import ingest_reference

    ingest_reference(tmp_path, "Uroperitoneum Paper", "# Study\n\nRatios.\n")
    ref = "reference:uroperitoneum-paper"
    now = datetime.datetime(2026, 6, 7, tzinfo=timezone.utc)
    src = "[[sessions/websocket_x.md#turn-4]]"
    # A patient the agent linked to the paper as a consulted source.
    write_entity(
        tmp_path, "patient:drako",
        [FieldPatch(kind="derived_from", value=ref, author="agent",
                    source_ref=src, at=now)],
        create=True, name="Drako",
    )
    # A concept the dream distilled from the paper.
    write_entity(
        tmp_path, "topic:uroperitoneum",
        [FieldPatch(kind="derived_from", value=ref, author="dream",
                    source_ref="[[references/uroperitoneum-paper.md]]", at=now)],
        create=True, name="Uroperitoneum",
    )
    d = get_reference_detail(tmp_path, "uroperitoneum-paper")
    assert d is not None
    rel = {e["ref"]: e["relation"] for e in d["entities"]}
    assert rel == {"patient:drako": "referenced", "topic:uroperitoneum": "distilled"}
    # distilled sorts first.
    assert d["entities"][0]["ref"] == "topic:uroperitoneum"


def test_get_reference_detail_undistilled_has_null_outline(tmp_path: Path) -> None:
    from durin.memory.reference import ingest_reference

    ingest_reference(tmp_path, "Raw Doc", "# H\n\nbody.\n")
    d = get_reference_detail(tmp_path, "raw-doc")
    assert d is not None
    assert d["outline"] is None
    assert d["entities"] == []
    assert d["chunks_preview"]  # chunks still previewed
