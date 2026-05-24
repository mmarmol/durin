"""Unit tests for the memory graph builder used by the webui graph view."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from durin.memory.aliases_cache import _clear_all
from durin.memory.entity_page import EntityPage
from durin.memory.graph import build_memory_graph
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
    )
    path = ws / "memory" / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


def _store(ws: Path, content: str, entities: list[str], day: int = 1) -> None:
    store_memory(
        ws,
        content=content,
        entities=entities,
        valid_from=datetime.date(2026, 5, day),
    )


# ---------------------------------------------------------------------------
# basic shape
# ---------------------------------------------------------------------------


def test_empty_workspace_returns_empty_graph(tmp_path: Path) -> None:
    g = build_memory_graph(tmp_path)
    assert g == {
        "nodes": [],
        "edges": [],
        "stats": {
            "node_count": 0,
            "edge_count": 0,
            "phantom_count": 0,
            "session_count": 0,
            "truncated_nodes": False,
            "truncated_edges": False,
            "types": [],
        },
    }


def test_single_page_no_entries(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo", aliases=["Marcelo"])
    g = build_memory_graph(tmp_path)
    assert g["stats"]["node_count"] == 1
    assert g["stats"]["edge_count"] == 0
    assert g["nodes"][0]["id"] == "person:marcelo"
    assert g["nodes"][0]["weight"] == 0


# ---------------------------------------------------------------------------
# weights + edges
# ---------------------------------------------------------------------------


def test_weight_counts_referencing_entries(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    for i in range(3):
        _store(tmp_path, f"obs {i}", ["person:marcelo"], day=i + 1)
    g = build_memory_graph(tmp_path)
    node = next(n for n in g["nodes"] if n["id"] == "person:marcelo")
    assert node["weight"] == 3


def test_cooccurrence_edge_weight(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    _write_page(tmp_path, "project", "durin")
    # 2 entries co-mention → edge weight 2
    _store(tmp_path, "marcelo + durin", ["person:marcelo", "project:durin"])
    _store(tmp_path, "again", ["person:marcelo", "project:durin"], day=2)
    g = build_memory_graph(tmp_path)
    assert len(g["edges"]) == 1
    e = g["edges"][0]
    assert e["weight"] == 2
    assert {e["source"], e["target"]} == {"person:marcelo", "project:durin"}


def test_no_edge_for_solo_entry(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    _store(tmp_path, "solo", ["person:marcelo"])
    g = build_memory_graph(tmp_path)
    assert g["edges"] == []


# ---------------------------------------------------------------------------
# phantom nodes — entry tagged a ref with no page
# ---------------------------------------------------------------------------


def test_phantom_node_for_unconsolidated_ref(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    _store(tmp_path, "with phantom", ["person:marcelo", "topic:newthing"])
    g = build_memory_graph(tmp_path)
    phantom = next(n for n in g["nodes"] if n.get("phantom"))
    assert phantom["id"] == "topic:newthing"
    assert phantom["type"] == "topic"
    assert g["stats"]["phantom_count"] == 1
    # Phantom still participates in edges.
    assert len(g["edges"]) == 1


# ---------------------------------------------------------------------------
# archive subfolder pages must NOT appear (de-indexed by design)
# ---------------------------------------------------------------------------


def test_archive_pages_excluded(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    # Simulate an archived absorbed page under canonical/archive/
    archive_dir = tmp_path / "memory" / "entities" / "person" / "marcelo" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    EntityPage(
        type="person", name="Old", aliases=[],
        extra={"absorbed_into": "../../marcelo.md"},
    ).save(archive_dir / "old.md")
    g = build_memory_graph(tmp_path)
    ids = {n["id"] for n in g["nodes"]}
    assert "person:marcelo" in ids
    assert not any("old" in i for i in ids), f"archive leaked: {ids}"


# ---------------------------------------------------------------------------
# caps + sort
# ---------------------------------------------------------------------------


def test_truncation_flagged_when_max_nodes_exceeded(tmp_path: Path) -> None:
    for i in range(12):
        _write_page(tmp_path, "topic", f"t{i:02d}")
    g = build_memory_graph(tmp_path, max_nodes=5)
    assert g["stats"]["node_count"] == 5
    assert g["stats"]["truncated_nodes"] is True


def test_nodes_sorted_by_weight_desc(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "low")
    _write_page(tmp_path, "person", "high")
    _store(tmp_path, "x", ["person:high"])
    _store(tmp_path, "y", ["person:high"], day=2)
    _store(tmp_path, "z", ["person:low"], day=3)
    g = build_memory_graph(tmp_path)
    # high (weight 2) before low (weight 1)
    assert g["nodes"][0]["id"] == "person:high"
    assert g["nodes"][1]["id"] == "person:low"


# ---------------------------------------------------------------------------
# stats.types is sorted + complete
# ---------------------------------------------------------------------------


def test_stats_types_sorted(tmp_path: Path) -> None:
    _write_page(tmp_path, "project", "p")
    _write_page(tmp_path, "person", "a")
    _write_page(tmp_path, "topic", "t")
    g = build_memory_graph(tmp_path)
    assert g["stats"]["types"] == ["person", "project", "topic"]


# ---------------------------------------------------------------------------
# session nodes + session→entity edges (added in the sessions-in-graph pass)
# ---------------------------------------------------------------------------


def _write_session(ws: Path, stem: str, *, messages: int = 0,
                    title: str | None = None,
                    meta_entities: list[str] | None = None) -> Path:
    """Write a minimal session jsonl + meta.json fixture."""
    import json
    sessions_dir = ws / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    jsonl = sessions_dir / f"{stem}.jsonl"
    lines: list[dict] = []
    if title:
        lines.append({"title": title, "channel": "websocket"})
    for i in range(messages):
        lines.append({"role": "user", "content": f"msg {i}"})
    jsonl.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                      encoding="utf-8")
    if meta_entities is not None:
        meta = sessions_dir / f"{stem}.meta.json"
        meta.write_text(json.dumps({
            "session_key": f"websocket:{stem}",
            "events": [],
            "derived": {"_last_tags": {"entities": meta_entities}},
        }), encoding="utf-8")
    return jsonl


def test_session_nodes_included_by_default(tmp_path: Path) -> None:
    _write_session(tmp_path, "sess_a", messages=5, title="My session")
    _write_session(tmp_path, "sess_b", messages=3)
    g = build_memory_graph(tmp_path)
    refs = {n["id"] for n in g["nodes"]}
    assert "session:sess_a" in refs
    assert "session:sess_b" in refs
    a = next(n for n in g["nodes"] if n["id"] == "session:sess_a")
    assert a["type"] == "session"
    assert a["name"] == "My session"
    assert a["weight"] == 5
    assert g["stats"]["session_count"] == 2


def test_session_nodes_excluded_when_flag_off(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    _write_session(tmp_path, "sess_a", messages=2)
    g = build_memory_graph(tmp_path, include_sessions=False)
    refs = {n["id"] for n in g["nodes"]}
    assert "session:sess_a" not in refs
    assert g["stats"]["session_count"] == 0


def test_session_to_entity_edge_from_source_refs(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    _write_session(tmp_path, "sess_a", messages=2)
    # Store entry with source_refs pointing back to sess_a
    store_memory(
        tmp_path, content="m discussed durin",
        entities=["person:marcelo"],
        source_refs=["sessions/sess_a.md#turn-3"],
        valid_from=datetime.date(2026, 5, 1),
    )
    g = build_memory_graph(tmp_path)
    sess_edges = [e for e in g["edges"]
                   if e["source"].startswith("session:")
                   or e["target"].startswith("session:")]
    assert len(sess_edges) == 1
    e = sess_edges[0]
    assert {e["source"], e["target"]} == {"session:sess_a", "person:marcelo"}
    assert e["weight"] == 1


def test_session_to_entity_edge_from_meta_tags(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "marcelo")
    _write_session(tmp_path, "sess_a", messages=2,
                    meta_entities=["person:marcelo", "project:durin"])
    # project:durin is phantom (no page) — should still render the edge.
    g = build_memory_graph(tmp_path)
    sess_edges = sorted(
        [(e["source"], e["target"]) for e in g["edges"]
         if e["source"].startswith("session:")
         or e["target"].startswith("session:")],
    )
    assert ("session:sess_a", "person:marcelo") in sess_edges
    assert ("session:sess_a", "project:durin") in sess_edges


def test_session_friendly_label_uuid(tmp_path: Path) -> None:
    """UUID-shaped stems get the channel abbrev + 8-char prefix."""
    _write_session(tmp_path, "websocket_12c54195-1548-4d76-925f-dc772b023f40",
                    messages=1)
    g = build_memory_graph(tmp_path)
    sess = next(n for n in g["nodes"] if n["type"] == "session")
    assert sess["name"] == "ws · 12c54195"


def test_session_friendly_label_short_suffix(tmp_path: Path) -> None:
    """Non-UUID short suffix is kept whole (cli_direct → cli · direct)."""
    _write_session(tmp_path, "cli_direct", messages=1)
    g = build_memory_graph(tmp_path)
    sess = next(n for n in g["nodes"] if n["type"] == "session")
    assert sess["name"] == "cli · direct"


def test_session_friendly_label_unknown_channel(tmp_path: Path) -> None:
    """Unknown channel prefix returns the stem unchanged (no surprise rename)."""
    _write_session(tmp_path, "weirdchannel_abc-def", messages=1)
    g = build_memory_graph(tmp_path)
    sess = next(n for n in g["nodes"] if n["type"] == "session")
    assert sess["name"] == "weirdchannel_abc-def"


def test_session_friendly_label_explicit_title_wins(tmp_path: Path) -> None:
    """If the identity block has display_name/title, that wins over the stem heuristic."""
    _write_session(tmp_path, "websocket_abcd1234-…", messages=1,
                    title="My Project Sync")
    g = build_memory_graph(tmp_path)
    sess = next(n for n in g["nodes"] if n["type"] == "session")
    assert sess["name"] == "My Project Sync"


def test_source_refs_and_meta_evidence_compound_weight(tmp_path: Path) -> None:
    _write_page(tmp_path, "person", "m")
    _write_session(tmp_path, "sess", messages=2,
                    meta_entities=["person:m"])  # +1 from meta
    store_memory(
        tmp_path, content="x", entities=["person:m"],
        source_refs=["sessions/sess.md#turn-1"],  # +1 from refs
        valid_from=datetime.date(2026, 5, 1),
    )
    g = build_memory_graph(tmp_path)
    sess_edges = [e for e in g["edges"]
                   if e["source"].startswith("session:")
                   or e["target"].startswith("session:")]
    assert len(sess_edges) == 1
    assert sess_edges[0]["weight"] == 2
