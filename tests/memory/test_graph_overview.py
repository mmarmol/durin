"""Tests for the overview builder: structure is semantic-only."""

from __future__ import annotations

import datetime
import pytest
from pathlib import Path

from durin.memory.aliases_cache import _clear_all as _clear_alias_cache
from durin.memory.entity_page import EntityPage
from durin.memory.store import store_memory
from durin.memory import graph_overview
from durin.memory.graph_overview import (
    HUB_COUNT,
    _extract_hubs,
    _semantic_view,
    build_overview,
    get_full_graph_cached,
)


@pytest.fixture(autouse=True)
def _reset_alias_cache():
    _clear_alias_cache()
    yield
    _clear_alias_cache()


@pytest.fixture(autouse=True)
def _reset_overview_cache():
    graph_overview._clear_all()
    yield
    graph_overview._clear_all()


def _node(nid: str, *, weight: int = 1, phantom: bool = False, ntype: str | None = None):
    t = ntype if ntype is not None else nid.partition(":")[0]
    d = {"id": nid, "type": t, "name": nid.partition(":")[2] or nid, "weight": weight}
    if phantom:
        d["phantom"] = True
    return d


def _edge(a: str, b: str, weight: int = 1, kind: str | None = None):
    e = {"source": a, "target": b, "weight": weight}
    if kind:
        e["kind"] = kind
    return e


def test_semantic_view_drops_sessions_and_phantoms():
    payload = {
        "nodes": [
            _node("person:alice", weight=5),
            _node("session:s1", weight=400, ntype="session"),
            _node("topic:ghost", weight=3, phantom=True),
            _node("reference:doc", weight=2, ntype="reference"),
        ],
        "edges": [
            _edge("person:alice", "session:s1", 9),
            _edge("person:alice", "topic:ghost", 2),
            _edge("person:alice", "reference:doc", 1, kind="derived_from"),
        ],
    }
    nodes, edges = _semantic_view(payload)
    ids = {n["id"] for n in nodes}
    assert ids == {"person:alice", "reference:doc"}
    assert edges == [
        {"source": "person:alice", "target": "reference:doc", "weight": 1, "kind": "derived_from"}
    ]


def test_extract_hubs_takes_top_weight_entities_only():
    nodes = [
        _node("company:mxhero", weight=90),
        _node("person:marcelo", weight=80),
        _node("reference:handbook", weight=99, ntype="reference"),
        _node("topic:minor", weight=1),
    ]
    hubs, rest = _extract_hubs(nodes, [], top_n=2)
    assert [h["id"] for h in hubs] == ["company:mxhero", "person:marcelo"]
    assert {n["id"] for n in rest} == {"reference:handbook", "topic:minor"}


def test_extract_hubs_deterministic_tie_break():
    nodes = [_node("topic:b", weight=5), _node("topic:a", weight=5)]
    hubs, _rest = _extract_hubs(nodes, [], top_n=1)
    assert [h["id"] for h in hubs] == ["topic:a"]


from durin.memory.graph_overview import _communities, _label_propagation


def _adj(edges: list[tuple[str, str, float]]) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = {}
    for a, b, w in edges:
        out.setdefault(a, []).append((b, w))
        out.setdefault(b, []).append((a, w))
    return out


def _two_cliques() -> tuple[list[str], dict[str, list[tuple[str, float]]]]:
    left = [f"topic:l{i}" for i in range(4)]
    right = [f"topic:r{i}" for i in range(4)]
    edges = []
    for grp in (left, right):
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                edges.append((grp[i], grp[j], 3.0))
    edges.append((left[0], right[0], 0.5))
    return left + right, _adj(edges)


def test_label_propagation_separates_two_cliques():
    ids, adj = _two_cliques()
    labels = _label_propagation(ids, adj)
    comms = _communities(labels)
    sizes = sorted(len(m) for m in comms.values())
    assert sizes == [4, 4]


def test_label_propagation_is_deterministic_under_input_order():
    ids, adj = _two_cliques()
    a = _label_propagation(ids, adj)
    b = _label_propagation(list(reversed(ids)), adj)
    assert a == b


def test_label_propagation_isolated_nodes_stay_singleton():
    labels = _label_propagation(["topic:lone"], {})
    assert labels == {"topic:lone": "topic:lone"}


from durin.memory.graph_overview import (
    BUBBLE_DISPLAY_CAP,
    BUBBLE_MIN_MEMBERS,
    LOOSE_DISPLAY_CAP,
    OTHERS_ID,
    assemble_overview,
)


def _community_payload(n_communities: int, size: int, *, hubs: int = 0):
    """Synthetic payload: n cliques of `size`, plus optional mega-hubs
    connected to every node (weight high enough to rank first)."""
    nodes, edges = [], []
    for c in range(n_communities):
        ids = [f"topic:c{c}n{i}" for i in range(size)]
        nodes += [_node(i, weight=2) for i in ids]
        for i in range(size):
            for j in range(i + 1, size):
                edges.append(_edge(ids[i], ids[j], 3))
    for h in range(hubs):
        hid = f"company:hub{h}"
        nodes.append(_node(hid, weight=1000))
        edges += [_edge(hid, n["id"], 1) for n in nodes if n["id"] != hid]
    return {"nodes": nodes, "edges": edges, "stats": {}}


def test_assemble_clustered_mode_with_bubbles_and_members():
    payload = _community_payload(3, BUBBLE_MIN_MEMBERS + 1)
    out = assemble_overview(payload)
    assert out["mode"] == "clustered"
    assert len(out["bubbles"]) == 3
    for b in out["bubbles"]:
        assert b["count"] == BUBBLE_MIN_MEMBERS + 1
        assert b["id"] in out["members"]
        assert len(out["members"][b["id"]]) == b["count"]
        assert b["id"] in out["members"][b["id"]]


def test_assemble_flat_mode_when_no_community_reaches_threshold():
    payload = _community_payload(4, max(2, BUBBLE_MIN_MEMBERS - 1))
    out = assemble_overview(payload)
    assert out["mode"] == "flat"
    assert out["bubbles"] == []


def test_hubs_extracted_before_clustering_prevent_blob():
    payload = _community_payload(2, BUBBLE_MIN_MEMBERS + 2, hubs=1)
    out = assemble_overview(payload)
    assert [h["id"] for h in out["hubs"]][0] == "company:hub0"
    assert len(out["bubbles"]) == 2


def test_aggregated_edges_reference_container_ids():
    payload = _community_payload(2, BUBBLE_MIN_MEMBERS + 1, hubs=1)
    out = assemble_overview(payload)
    containers = (
        {b["id"] for b in out["bubbles"]}
        | {h["id"] for h in out["hubs"]}
        | {n["id"] for n in out["loose"]}
    )
    assert out["edges"], "hub touches every community: aggregated edges expected"
    for e in out["edges"]:
        assert e["source"] in containers and e["target"] in containers
        assert e["source"] != e["target"]


def test_bubble_display_cap_overflows_into_others():
    payload = _community_payload(BUBBLE_DISPLAY_CAP + 3, BUBBLE_MIN_MEMBERS + 1)
    out = assemble_overview(payload)
    assert len(out["bubbles"]) == BUBBLE_DISPLAY_CAP + 1
    others = [b for b in out["bubbles"] if b["id"] == OTHERS_ID]
    assert len(others) == 1
    assert others[0]["count"] == 3 * (BUBBLE_MIN_MEMBERS + 1)
    assert len(out["members"][OTHERS_ID]) == others[0]["count"]


def test_assemble_empty_payload_is_flat_with_zero_stats():
    out = assemble_overview({"nodes": [], "edges": [], "stats": {}})
    assert out["mode"] == "flat"
    assert out["stats"]["entity_count"] == 0
    assert out["bubbles"] == [] and out["hubs"] == [] and out["loose"] == []


def test_session_and_phantom_totals_survive_in_stats():
    payload = _community_payload(1, BUBBLE_MIN_MEMBERS + 1)
    payload["nodes"] += [
        _node("session:s1", weight=400, ntype="session"),
        _node("topic:ghost", weight=1, phantom=True),
    ]
    out = assemble_overview(payload)
    assert out["stats"]["session_count"] == 1
    assert out["stats"]["phantom_count"] == 1
    assert out["stats"]["entity_count"] == BUBBLE_MIN_MEMBERS + 1


def test_hub_extraction_is_capped_at_hub_count():
    nodes = [_node(f"person:h{i:02d}", weight=100) for i in range(HUB_COUNT + 10)]
    nodes += [_node(f"topic:t{i:02d}", weight=2) for i in range(40)]
    out = assemble_overview({"nodes": nodes, "edges": [], "stats": {}})
    assert len(out["hubs"]) == HUB_COUNT


def test_uniform_communities_with_noise_floor_keep_all_bubbles():
    payload = _community_payload(6, BUBBLE_MIN_MEMBERS + 1)
    payload["nodes"] += [_node(f"topic:stray{i}", weight=1) for i in range(5)]
    out = assemble_overview(payload)
    assert out["mode"] == "clustered"
    assert len(out["bubbles"]) == 6
    assert out["hubs"] == []


def test_tiered_weights_extract_only_true_outliers():
    payload = _community_payload(2, BUBBLE_MIN_MEMBERS + 2, hubs=1)
    payload["nodes"] += [_node(f"topic:mid{i}", weight=3) for i in range(4)]
    out = assemble_overview(payload)
    assert [h["id"] for h in out["hubs"]] == ["company:hub0"]
    assert len(out["bubbles"]) == 2


def test_loose_nodes_are_display_capped_with_overflow_in_others():
    payload = _community_payload(1, BUBBLE_MIN_MEMBERS + 1)
    payload["nodes"] += [
        _node(f"topic:iso{i:03d}", weight=2) for i in range(LOOSE_DISPLAY_CAP + 25)
    ]
    out = assemble_overview(payload)
    assert out["mode"] == "clustered"
    assert len(out["loose"]) == LOOSE_DISPLAY_CAP
    others = [b for b in out["bubbles"] if b["id"] == OTHERS_ID]
    assert len(others) == 1
    assert others[0]["count"] == 25
    assert len(out["members"][OTHERS_ID]) == 25
    assert out["stats"]["loose_count"] == LOOSE_DISPLAY_CAP + 25


def test_others_bubble_merges_bubble_and_loose_overflow():
    payload = _community_payload(BUBBLE_DISPLAY_CAP + 2, BUBBLE_MIN_MEMBERS + 1)
    payload["nodes"] += [
        _node(f"topic:iso{i:03d}", weight=2) for i in range(LOOSE_DISPLAY_CAP + 5)
    ]
    out = assemble_overview(payload)
    others = [b for b in out["bubbles"] if b["id"] == OTHERS_ID]
    assert len(others) == 1
    expected = 2 * (BUBBLE_MIN_MEMBERS + 1) + 5
    assert others[0]["count"] == expected
    assert len(out["members"][OTHERS_ID]) == expected


def _write_page(ws: Path, type_: str, slug: str) -> None:
    page = EntityPage(type=type_, name=slug.title())
    page.save(ws / "memory" / "entities" / type_ / f"{slug}.md")


def test_cached_payload_rebuilds_only_on_tree_change(tmp_path, monkeypatch):
    _write_page(tmp_path, "person", "alice")
    store_memory(
        tmp_path,
        content="alice did a thing",
        entities=["person:alice"],
        valid_from=datetime.date(2026, 5, 1),
    )
    calls = {"n": 0}
    real = graph_overview.build_memory_graph

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(graph_overview, "build_memory_graph", counting)
    first = get_full_graph_cached(tmp_path)
    again = get_full_graph_cached(tmp_path)
    assert calls["n"] == 1
    assert again is first

    _write_page(tmp_path, "person", "bob")
    changed = get_full_graph_cached(tmp_path)
    assert calls["n"] == 2
    assert {n["id"] for n in changed["nodes"]} >= {"person:alice", "person:bob"}


def test_build_overview_smoke_on_real_workspace(tmp_path):
    for i in range(3):
        _write_page(tmp_path, "topic", f"t{i}")
    store_memory(
        tmp_path,
        content="t0 and t1 together",
        entities=["topic:t0", "topic:t1"],
        valid_from=datetime.date(2026, 5, 2),
    )
    out = build_overview(tmp_path)
    assert out["mode"] == "flat"
    assert out["stats"]["entity_count"] == 3


def test_build_overview_is_cached_and_invalidates_on_tree_change(tmp_path, monkeypatch):
    _write_page(tmp_path, "person", "alice")
    store_memory(
        tmp_path,
        content="alice did a thing",
        entities=["person:alice"],
        valid_from=datetime.date(2026, 5, 4),
    )
    calls = {"n": 0}
    real = graph_overview.assemble_overview

    def counting(payload):
        calls["n"] += 1
        return real(payload)

    monkeypatch.setattr(graph_overview, "assemble_overview", counting)
    first = build_overview(tmp_path)
    again = build_overview(tmp_path)
    assert calls["n"] == 1
    assert again is first
    _write_page(tmp_path, "person", "bob")
    build_overview(tmp_path)
    assert calls["n"] == 2


def test_refreshing_existing_workspace_does_not_evict_others(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_overview, "_CACHE_MAX", 2)
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    for ws, slug in ((ws_a, "alice"), (ws_b, "bob")):
        ws.mkdir()
        _write_page(ws, "person", slug)
    get_full_graph_cached(ws_a)
    get_full_graph_cached(ws_b)
    _write_page(ws_b, "person", "carol")
    get_full_graph_cached(ws_b)
    assert ws_a.resolve() in graph_overview._cache
    assert len(graph_overview._cache) == 2


from durin.memory.graph_overview import build_cluster_subgraph


def _clustered_workspace(tmp_path: Path) -> str:
    """Real workspace with one bubble-sized community; returns its rep ref."""
    n = BUBBLE_MIN_MEMBERS + 2
    for i in range(n):
        _write_page(tmp_path, "topic", f"m{i}")
    for i in range(n - 1):
        store_memory(
            tmp_path,
            content=f"m{i} with m{i + 1} and m0",
            entities=[f"topic:m{i}", f"topic:m{i + 1}", "topic:m0"],
            valid_from=datetime.date(2026, 5, 3),
        )
    out = build_overview(tmp_path)
    assert out["mode"] == "clustered"
    return out["bubbles"][0]["id"]


def test_cluster_subgraph_returns_members_with_focus(tmp_path):
    rep = _clustered_workspace(tmp_path)
    sub = build_cluster_subgraph(tmp_path, rep)
    ids = {n["id"] for n in sub["nodes"]}
    assert rep in ids
    assert sub["focus"] == rep
    # m0 co-mentions every other member, so its weight (n - 1) clears the hub
    # floor and it is extracted as a hub rather than joining the bubble — the
    # bubble itself holds the remaining BUBBLE_MIN_MEMBERS + 1 members.
    assert sub["total_members"] == BUBBLE_MIN_MEMBERS + 1
    for n in sub["nodes"]:
        member = n["id"].startswith("topic:m")
        scaffolding = bool(n.get("phantom")) or n["type"] == "session"
        assert member or scaffolding


def test_cluster_subgraph_unknown_ref_raises(tmp_path):
    _clustered_workspace(tmp_path)
    with pytest.raises(KeyError):
        build_cluster_subgraph(tmp_path, "topic:not-a-bubble")


def test_cluster_subgraph_caps_and_reports_total(tmp_path):
    rep = _clustered_workspace(tmp_path)
    sub = build_cluster_subgraph(tmp_path, rep, max_members=5)
    member_nodes = [n for n in sub["nodes"] if not n.get("phantom") and n["type"] != "session"]
    assert len(member_nodes) <= 5
    assert sub["total_members"] == BUBBLE_MIN_MEMBERS + 1
