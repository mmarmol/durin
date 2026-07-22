"""Overview builder: cluster bubbles + semantic hubs for the memory graph.

Structure is computed from SEMANTIC evidence only: entity-entity co-mention
edges and typed relations between consolidated pages (plus reference
``derived_from`` links). Sessions and phantoms never enter clustering input,
hub ranking, or size computation — session weight is a mechanical message
count, sessions edge into everything they touched, and with few sessions the
"communities" would degenerate into "what session A touched".
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

HUB_COUNT = 20
BUBBLE_MIN_MEMBERS = 15
BUBBLE_DISPLAY_CAP = 30

OTHERS_ID = "__others__"


def _semantic_view(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Nodes eligible for structure and the semantic edges between them.

    Drops phantom nodes (unconsolidated mentions) and session nodes.
    Reference nodes stay: they cluster along their derived_from neighbours
    but are not hub-eligible.
    """
    nodes = [
        n
        for n in payload["nodes"]
        if not n.get("phantom") and n["type"] != "session"
    ]
    keep = {n["id"] for n in nodes}
    edges = [
        e
        for e in payload["edges"]
        if e["source"] in keep and e["target"] in keep
    ]
    return nodes, edges


def _extract_hubs(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    top_n: int = HUB_COUNT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Top entities by semantic weight, removed before clustering.

    Mega-connectors bridge every community and would merge them into one
    blob, so they are drawn individually instead. References hold real
    content but are consultation material, not connectors — never hubs.
    """
    eligible = sorted(
        (n for n in nodes if n["type"] != "reference"),
        key=lambda n: (-int(n["weight"]), n["id"]),
    )
    hubs = eligible[:top_n]
    hub_ids = {n["id"] for n in hubs}
    rest = [n for n in nodes if n["id"] not in hub_ids]
    return hubs, rest


def _label_propagation(
    node_ids: list[str],
    adj: dict[str, list[tuple[str, float]]],
    max_rounds: int = 20,
) -> dict[str, str]:
    """Weighted label propagation, fully deterministic.

    Every source of nondeterminism in the textbook algorithm is pinned:
    nodes iterate in sorted-id order, each label starts as the node's own
    id, and neighbour-label ties break lexicographically. Same input ⇒
    same partition, regardless of input list order.
    """
    order = sorted(node_ids)
    labels = {nid: nid for nid in order}
    for _ in range(max_rounds):
        changed = False
        for nid in order:
            counts: dict[str, float] = defaultdict(float)
            for other, weight in adj.get(nid, ()):  # noqa: B909 — read-only
                counts[labels[other]] += float(weight)
            if not counts:
                continue
            best = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if best != labels[nid]:
                labels[nid] = best
                changed = True
        if not changed:
            break
    return labels


def _communities(labels: dict[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for nid, label in labels.items():
        out[label].append(nid)
    return {label: sorted(members) for label, members in out.items()}
