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
