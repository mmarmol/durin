"""Tests for the overview builder: structure is semantic-only."""

from __future__ import annotations

import pytest

from durin.memory.graph_overview import (
    HUB_COUNT,
    _extract_hubs,
    _semantic_view,
)


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
