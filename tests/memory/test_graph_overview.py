"""Tests for the overview builder: structure is semantic-only."""

from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import pytest

from durin.memory import graph_overview
from durin.memory.aliases_cache import _clear_all as _clear_alias_cache
from durin.memory.entity_page import EntityPage
from durin.memory.graph_overview import (
    HUB_COUNT,
    _entity_scores,
    _extract_hubs,
    _semantic_view,
    build_overview,
    get_full_graph_cached,
)
from durin.memory.store import store_memory


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
        _node("company:mxhero"),
        _node("person:marcelo"),
        _node("reference:handbook", ntype="reference"),
        _node("topic:minor"),
    ]
    scores = {
        "company:mxhero": 90.0,
        "person:marcelo": 80.0,
        "reference:handbook": 99.0,
        "topic:minor": 1.0,
    }
    hubs, rest = _extract_hubs(nodes, scores, top_n=2)
    assert [h["id"] for h in hubs] == ["company:mxhero", "person:marcelo"]
    assert {n["id"] for n in rest} == {"reference:handbook", "topic:minor"}


def test_extract_hubs_deterministic_tie_break():
    nodes = [_node("topic:b"), _node("topic:a")]
    scores = {"topic:b": 5.0, "topic:a": 5.0}
    hubs, _rest = _extract_hubs(nodes, scores, top_n=1)
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
    connected to every node (degree high enough to rank first)."""
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
    # Weight is a dead signal on real data — hub-worthiness now comes from
    # degree, so the candidates need actual edges, not a raw weight field.
    hub_ids = [f"person:h{i:02d}" for i in range(HUB_COUNT + 10)]
    nodes = [_node(hid) for hid in hub_ids]
    nodes += [_node(f"topic:t{i:02d}") for i in range(40)]
    edges = [
        _edge(hub_ids[i], hub_ids[j])
        for i in range(len(hub_ids))
        for j in range(i + 1, len(hub_ids))
    ]
    out = assemble_overview({"nodes": nodes, "edges": edges, "stats": {}})
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
    # A ring (not a full clique): every member has degree 2, low enough to
    # stay under the hub floor even though the isolated iso nodes below
    # outnumber it and drag the population median to 0 — a full clique's
    # degree (size - 1) would clear that floor and get extracted as hubs
    # instead of forming the one bubble this test is about.
    size = BUBBLE_MIN_MEMBERS + 1
    ring = [f"topic:ring{i}" for i in range(size)]
    nodes = [_node(nid) for nid in ring]
    edges = [_edge(ring[i], ring[(i + 1) % size]) for i in range(size)]
    nodes += [_node(f"topic:iso{i:03d}") for i in range(LOOSE_DISPLAY_CAP + 25)]
    payload = {"nodes": nodes, "edges": edges, "stats": {}}
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


# ---------------------------------------------------------------------------
# group_by: "type" partitions the remainder by node type instead of
# label-propagation communities. Hub extraction runs identically first.
# ---------------------------------------------------------------------------


def test_type_grouping_partitions_remainder_by_type_field():
    topics = [_node(f"topic:t{i}", weight=2) for i in range(BUBBLE_MIN_MEMBERS + 1)]
    vendors = [_node(f"vendor:v{i}", weight=2) for i in range(BUBBLE_MIN_MEMBERS + 2)]
    tickets = [_node(f"ticket:k{i}", weight=2) for i in range(2)]
    payload = {"nodes": topics + vendors + tickets, "edges": [], "stats": {}}

    out = assemble_overview(payload, group_by="type")

    assert out["mode"] == "clustered"
    bubble_ids = {b["id"] for b in out["bubbles"]}
    assert bubble_ids == {"type:topic", "type:vendor"}
    for b in out["bubbles"]:
        name = b["id"].split(":", 1)[1]
        assert b["name"] == name
        assert b["types"] == [name]
        assert b["id"] in out["members"]
        assert len(out["members"][b["id"]]) == b["count"]
    loose_ids = {n["id"] for n in out["loose"]}
    assert {"ticket:k0", "ticket:k1"} <= loose_ids


def test_hubs_are_identical_regardless_of_group_by():
    # Same fixture (one mega-hub + two cliques) assembled both ways: hub
    # extraction happens before the grouping choice is even consulted, so
    # the hub list must not depend on it.
    payload = _community_payload(2, BUBBLE_MIN_MEMBERS + 2, hubs=1)
    by_community = assemble_overview(payload, group_by="community")
    by_type = assemble_overview(payload, group_by="type")
    assert by_community["hubs"] == by_type["hubs"]
    assert by_community["hubs"][0]["id"] == "company:hub0"


def test_assemble_overview_unknown_group_by_raises():
    payload = _community_payload(1, BUBBLE_MIN_MEMBERS + 1)
    with pytest.raises(ValueError):
        assemble_overview(payload, group_by="bogus")


def test_type_grouping_flat_mode_when_no_type_reaches_threshold():
    # Same flat-mode rule as community grouping: nothing reaches
    # BUBBLE_MIN_MEMBERS, so the payload falls back to the plain graph.
    small = [_node(f"topic:t{i}", weight=1) for i in range(BUBBLE_MIN_MEMBERS - 1)]
    out = assemble_overview({"nodes": small, "edges": [], "stats": {}}, group_by="type")
    assert out["mode"] == "flat"
    assert out["bubbles"] == []


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

    def counting(payload, group_by="community"):
        calls["n"] += 1
        return real(payload, group_by=group_by)

    monkeypatch.setattr(graph_overview, "assemble_overview", counting)
    first = build_overview(tmp_path)
    again = build_overview(tmp_path)
    assert calls["n"] == 1
    assert again is first
    _write_page(tmp_path, "person", "bob")
    build_overview(tmp_path)
    assert calls["n"] == 2


def test_build_overview_caches_each_group_by_independently(tmp_path, monkeypatch):
    _write_page(tmp_path, "person", "alice")
    store_memory(
        tmp_path,
        content="alice did a thing",
        entities=["person:alice"],
        valid_from=datetime.date(2026, 5, 4),
    )
    calls = {"n": 0}
    real = graph_overview.assemble_overview

    def counting(payload, group_by="community"):
        calls["n"] += 1
        return real(payload, group_by=group_by)

    monkeypatch.setattr(graph_overview, "assemble_overview", counting)

    community_first = build_overview(tmp_path, group_by="community")
    community_again = build_overview(tmp_path, group_by="community")
    assert calls["n"] == 1
    assert community_again is community_first

    # A different group_by is a cache miss, not a hit on community's slot —
    # and its own repeat call hits its own cache rather than recomputing.
    type_first = build_overview(tmp_path, group_by="type")
    type_again = build_overview(tmp_path, group_by="type")
    assert calls["n"] == 2
    assert type_again is type_first
    assert type_first is not community_first


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
    # m0 co-mentions every other member, so it has a relation/co-occurrence
    # edge to each of them (degree n - 1) — enough to clear the hub floor —
    # and it is extracted as a hub rather than joining the bubble. The
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


def test_cluster_scaffolding_does_not_cascade(tmp_path):
    # Ghost type is "vendor" (not "person"): build_memory_graph sorts edges
    # by (-weight, source, target), and both new edges share source
    # "vendor:ghost-a" once "topic:m1" outranks it alphabetically as a
    # source — that ordering puts the member-adjacent edge before the
    # ghost-to-ghost edge in the single iteration pass, which is what lets
    # the pre-fix code's mutate-while-iterating bug actually cascade.
    rep = _clustered_workspace(tmp_path)
    store_memory(
        tmp_path,
        content="member with ghost one",
        entities=["topic:m1", "vendor:ghost-a"],
        valid_from=datetime.date(2026, 5, 10),
    )
    store_memory(
        tmp_path,
        content="ghost one with ghost two",
        entities=["vendor:ghost-a", "vendor:ghost-b"],
        valid_from=datetime.date(2026, 5, 11),
    )
    sub = build_cluster_subgraph(tmp_path, rep)
    ids = {n["id"] for n in sub["nodes"]}
    assert "vendor:ghost-a" in ids
    assert "vendor:ghost-b" not in ids


def test_cluster_subgraph_uses_single_tree_snapshot(tmp_path, monkeypatch):
    rep = _clustered_workspace(tmp_path)
    graph_overview._clear_all()
    calls = {"n": 0}
    real = graph_overview._tree_signature

    def counting(ws):
        calls["n"] += 1
        return real(ws)

    monkeypatch.setattr(graph_overview, "_tree_signature", counting)
    build_cluster_subgraph(tmp_path, rep)
    assert calls["n"] == 1


def _typed_workspace(tmp_path: Path) -> str:
    """Real workspace with one same-typed group above the bubble threshold;
    returns its type-bubble id (`type:topic`). No relations needed — a type
    group is a plain field partition, not a co-mention community."""
    n = BUBBLE_MIN_MEMBERS + 2
    for i in range(n):
        _write_page(tmp_path, "topic", f"g{i}")
    out = build_overview(tmp_path, group_by="type")
    assert out["mode"] == "clustered"
    return next(b["id"] for b in out["bubbles"] if b["id"] == "type:topic")


def test_cluster_subgraph_drills_into_a_type_bubble(tmp_path):
    bubble_id = _typed_workspace(tmp_path)
    sub = build_cluster_subgraph(tmp_path, bubble_id, group_by="type")
    assert sub["focus"] == bubble_id
    assert sub["total_members"] == BUBBLE_MIN_MEMBERS + 2
    ids = {n["id"] for n in sub["nodes"]}
    assert all(nid.startswith("topic:g") for nid in ids)


def test_scores_rank_by_degree_and_damp_session_churn_not_dead_weight():
    """Production shape: legacy entry classes are empty (every node's
    ``weight`` field is 0), so the real signal is relation degree plus
    log-damped session evidence, not the dead weight field."""
    community_a = [f"topic:a{i}" for i in range(8)]
    community_b = [f"topic:b{i}" for i in range(8)]
    steady_neighbors = [f"topic:s{i}" for i in range(12)]

    nodes = [
        _node(nid, weight=0)
        for nid in community_a + community_b + steady_neighbors
    ]
    nodes += [
        _node("person:director", weight=0),
        _node("topic:steady", weight=0),
        _node("ticket:churny", weight=0),
    ]
    edges = []
    for group in (community_a, community_b):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                edges.append(_edge(group[i], group[j], kind="relation"))
    edges += [
        _edge("person:director", member, kind="relation")
        for member in community_a + community_b
    ]
    edges += [_edge("topic:steady", nid, kind="relation") for nid in steady_neighbors]

    session_nodes = [_node("session:s1", ntype="session")]
    session_edges = [_edge("session:s1", "topic:s0"), _edge("session:s1", "topic:s1")]
    for i in range(50):
        session_nodes.append(_node(f"session:churn{i}", ntype="session"))
        session_edges.append(_edge(f"session:churn{i}", "ticket:churny"))

    payload = {
        "nodes": nodes + session_nodes,
        "edges": edges + session_edges,
        "stats": {},
    }

    # (c) log damping: 50 sessions on a degree-0 entity score below a
    # moderate-degree (12) entity with no session evidence at all.
    sem_nodes, sem_edges = _semantic_view(payload)
    scores = _entity_scores(payload, sem_nodes, sem_edges)
    assert scores["topic:steady"] == pytest.approx(12.0)
    assert scores["ticket:churny"] == pytest.approx(
        graph_overview.SESSION_LOG_FACTOR * math.log1p(50)
    )
    assert scores["ticket:churny"] < scores["topic:steady"]

    out = assemble_overview(payload)

    # (a) two 8-member relation communities cluster at the new threshold.
    assert out["mode"] == "clustered"
    assert len(out["members"]["topic:a0"]) == 8

    # (b) the high-degree entity becomes a hub despite its dead weight field.
    director_in = next(n for n in payload["nodes"] if n["id"] == "person:director")
    assert director_in["weight"] == 0
    hub_ids = {h["id"] for h in out["hubs"]}
    assert "person:director" in hub_ids

    # (d) emitted hub/loose weights carry the score, not the original 0.
    director_out = next(h for h in out["hubs"] if h["id"] == "person:director")
    assert director_out["weight"] == round(scores["person:director"], 1)
    assert director_out["weight"] > 0
    loose_ids = {n["id"] for n in out["loose"]}
    assert "ticket:churny" in loose_ids
    churny_out = next(n for n in out["loose"] if n["id"] == "ticket:churny")
    assert churny_out["weight"] == round(scores["ticket:churny"], 1)
    assert churny_out["weight"] > 0


def _write_session(ws: Path, stem: str, *, meta_entities: list[str] | None = None) -> None:
    """Minimal session jsonl + meta.json fixture — just enough for the
    meta-tag evidence path (`meta.json::derived._last_tags.entities`), the
    dream-curator-populated route to a session→entity edge without any
    entry `source_refs` at all."""
    sessions_dir = ws / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{stem}.jsonl").write_text(
        json.dumps({"title": stem, "channel": "websocket"}) + "\n",
        encoding="utf-8",
    )
    if meta_entities is not None:
        (sessions_dir / f"{stem}.meta.json").write_text(
            json.dumps({"derived": {"_last_tags": {"entities": meta_entities}}}),
            encoding="utf-8",
        )


def test_build_overview_finds_hubs_from_relations_and_sessions_with_no_entries(tmp_path):
    """Anti-unfaithful-fixture regression: a production-shaped workspace has
    NO episodic/stable/corpus entries at all (the legacy entry classes are
    empty, so every node's weight is 0) — only dream-written relations and
    session evidence. The overview must still surface hubs instead of the
    old all-weight-0 blank map.
    """
    topics = [f"t{i}" for i in range(10)]
    for slug in topics:
        _write_page(tmp_path, "topic", slug)
    EntityPage(
        type="person",
        name="Director",
        relations=[{"to": f"topic:{slug}", "type": "oversees"} for slug in topics],
    ).save(tmp_path / "memory" / "entities" / "person" / "director.md")
    _write_session(tmp_path, "sess_a", meta_entities=["person:director", "topic:t0"])

    out = build_overview(tmp_path)
    assert out["stats"]["entity_count"] == 11
    assert len(out["hubs"]) > 0
    assert out["hubs"][0]["id"] == "person:director"
    assert out["hubs"][0]["weight"] > 0
