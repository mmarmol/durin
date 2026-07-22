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


def _build_adjacency(
    edges: list[dict[str, Any]],
) -> dict[str, list[tuple[str, float]]]:
    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        w = float(e.get("weight", 1))
        adj[e["source"]].append((e["target"], w))
        adj[e["target"]].append((e["source"], w))
    return adj


def assemble_overview(payload: dict[str, Any]) -> dict[str, Any]:
    """Aggregate the full uncapped graph payload into the overview shape.

    Hubs come out first (they bridge everything); label propagation runs on
    the rest; communities >= BUBBLE_MIN_MEMBERS become bubbles keyed by
    their heaviest member; the rest stay as loose nodes. Edges are summed
    between containers. mode="flat" when no community reaches the
    threshold — small or young workspaces render the plain graph instead.
    """
    sem_nodes, sem_edges = _semantic_view(payload)
    by_id = {n["id"]: n for n in sem_nodes}

    # Extract hubs only when there are nodes with significantly higher weight.
    # Find the largest weight gap and extract only nodes above that gap.
    non_ref_nodes = [n for n in sem_nodes if n["type"] != "reference"]
    if non_ref_nodes:
        weights_sorted = sorted(set(n["weight"] for n in non_ref_nodes), reverse=True)
        if len(weights_sorted) <= 1:
            # All weights equal → no hubs
            hubs, rest = [], sem_nodes
        else:
            # Find the largest gap between consecutive weight levels
            gaps = [
                (weights_sorted[i], weights_sorted[i + 1], weights_sorted[i] - weights_sorted[i + 1])
                for i in range(len(weights_sorted) - 1)
            ]
            max_gap_idx = max(range(len(gaps)), key=lambda i: gaps[i][2])
            cutoff_weight = gaps[max_gap_idx][1]  # Extract all nodes with weight > cutoff
            # Only extract as hubs the nodes strictly above the cutoff
            hub_count = sum(
                1 for n in non_ref_nodes if n["weight"] > cutoff_weight
            )
            hubs, rest = _extract_hubs(sem_nodes, sem_edges, top_n=hub_count)
    else:
        hubs, rest = [], sem_nodes
    hub_ids = {n["id"] for n in hubs}
    rest_ids = [n["id"] for n in rest]
    rest_edges = [
        e
        for e in sem_edges
        if e["source"] not in hub_ids and e["target"] not in hub_ids
    ]
    labels = _label_propagation(rest_ids, _build_adjacency(rest_edges))
    comms = _communities(labels)

    def _rep(members: list[str]) -> str:
        return min(members, key=lambda m: (-int(by_id[m]["weight"]), m))

    sized = sorted(
        (members for members in comms.values() if len(members) >= BUBBLE_MIN_MEMBERS),
        key=lambda m: (-len(m), _rep(m)),
    )
    loose_ids = sorted(
        nid
        for members in comms.values()
        if len(members) < BUBBLE_MIN_MEMBERS
        for nid in members
    )

    stats = {
        "entity_count": sum(
            1 for n in sem_nodes if n["type"] != "reference"
        ),
        "reference_count": sum(1 for n in sem_nodes if n["type"] == "reference"),
        "phantom_count": sum(1 for n in payload["nodes"] if n.get("phantom")),
        "session_count": sum(
            1 for n in payload["nodes"] if n["type"] == "session"
        ),
        "bubble_count": 0,
        "loose_count": len(loose_ids),
    }
    if not sized:
        return {
            "mode": "flat",
            "bubbles": [],
            "hubs": [],
            "loose": [],
            "edges": [],
            "members": {},
            "stats": stats,
        }

    shown, overflow = sized[:BUBBLE_DISPLAY_CAP], sized[BUBBLE_DISPLAY_CAP:]
    members_map: dict[str, list[str]] = {}
    bubbles: list[dict[str, Any]] = []

    def _bubble(bid: str, name: str, members: list[str]) -> dict[str, Any]:
        top = sorted(members, key=lambda m: (-int(by_id[m]["weight"]), m))[:5]
        type_counts: dict[str, int] = defaultdict(int)
        for m in members:
            type_counts[by_id[m]["type"]] += 1
        types = [
            t
            for t, _c in sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        ]
        members_map[bid] = members
        return {
            "id": bid,
            "name": name,
            "count": len(members),
            "types": types,
            "top": [
                {
                    "id": m,
                    "name": by_id[m]["name"],
                    "type": by_id[m]["type"],
                    "weight": by_id[m]["weight"],
                }
                for m in top
            ],
        }

    for members in shown:
        rep = _rep(members)
        bubbles.append(_bubble(rep, by_id[rep]["name"], members))
    if overflow:
        merged = sorted(nid for members in overflow for nid in members)
        bubbles.append(_bubble(OTHERS_ID, "__others__", merged))
    stats["bubble_count"] = len(bubbles)

    container_of: dict[str, str] = {}
    for bid, members in members_map.items():
        for m in members:
            container_of[m] = bid
    for hid in hub_ids:
        container_of[hid] = hid
    for nid in loose_ids:
        container_of[nid] = nid

    agg: dict[tuple[str, str], float] = defaultdict(float)
    for e in sem_edges:
        ca, cb = container_of.get(e["source"]), container_of.get(e["target"])
        if ca is None or cb is None or ca == cb:
            continue
        key = (ca, cb) if ca < cb else (cb, ca)
        agg[key] += float(e.get("weight", 1))
    edges = [
        {"source": a, "target": b, "weight": round(w, 2)}
        for (a, b), w in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    return {
        "mode": "clustered",
        "bubbles": bubbles,
        "hubs": hubs,
        "loose": [by_id[nid] for nid in loose_ids],
        "edges": edges,
        "members": members_map,
        "stats": stats,
    }
