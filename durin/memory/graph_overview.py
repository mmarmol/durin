"""Overview builder: cluster bubbles + semantic hubs for the memory graph.

Structure is computed from SEMANTIC evidence only: entity-entity co-mention
edges and typed relations between consolidated pages (plus reference
``derived_from`` links). Sessions and phantoms never enter clustering input,
hub ranking, or size computation — session weight is a mechanical message
count, sessions edge into everything they touched, and with few sessions the
"communities" would degenerate into "what session A touched". A node
qualifies as a hub only when it is a clear outlier — at least twice the
median semantic weight — so uniform or young graphs surface no hubs at all,
and the qualifying count is always capped at HUB_COUNT.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from durin.memory.graph import build_memory_graph
from durin.memory.paths import walk_class

HUB_COUNT = 20
BUBBLE_MIN_MEMBERS = 15
BUBBLE_DISPLAY_CAP = 30
LOOSE_DISPLAY_CAP = 30

OTHERS_ID = "__others__"

_UNCAPPED_NODES = 100_000
_UNCAPPED_EDGES = 400_000
_CACHE_MAX = 4

_cache: dict[Path, tuple[int, dict[str, Any]]] = {}
_overview_cache: dict[Path, tuple[int, dict[str, Any]]] = {}
_lock = threading.Lock()

_ENTRY_CLASSES = ("episodic", "stable", "corpus")


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

    Hubs come out first (they bridge everything): an entity qualifies only
    when its weight is at least twice the median semantic weight (floor of
    3), and the qualifying count is capped at HUB_COUNT — so uniform or
    young graphs extract no hubs at all. Label propagation then runs on the
    rest; communities >= BUBBLE_MIN_MEMBERS become bubbles keyed by their
    heaviest member; the rest stay as loose nodes, display-capped at
    LOOSE_DISPLAY_CAP by weight, with any overflow folded into the same
    others bubble that absorbs bubble overflow. Edges are summed between
    containers. mode="flat" when no community reaches the threshold — small
    or young workspaces render the plain graph instead, though hubs (found
    before clustering is attempted) still populate when present.
    """
    sem_nodes, sem_edges = _semantic_view(payload)
    by_id = {n["id"]: n for n in sem_nodes}

    # Hub qualification: a hub is an entity clearly heavier than the typical
    # node — at least twice the median semantic weight (and never below 3).
    # Uniform or young graphs therefore extract no hubs at all (nothing is
    # an outlier), and the count is hard-capped so the overview stays small.
    non_ref_nodes = [n for n in sem_nodes if n["type"] != "reference"]
    if non_ref_nodes:
        weights = sorted(int(n["weight"]) for n in non_ref_nodes)
        median = weights[len(weights) // 2]
        floor = max(3, 2 * median)
        qualifying = sum(1 for n in non_ref_nodes if int(n["weight"]) >= floor)
        hubs, rest = _extract_hubs(
            sem_nodes, sem_edges, top_n=min(HUB_COUNT, qualifying)
        )
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
            "hubs": hubs,
            "loose": [],
            "edges": [],
            "members": {},
            "stats": stats,
        }

    loose_by_weight = sorted(
        loose_ids, key=lambda nid: (-int(by_id[nid]["weight"]), nid)
    )
    displayed_loose_ids = loose_by_weight[:LOOSE_DISPLAY_CAP]
    loose_overflow_ids = loose_by_weight[LOOSE_DISPLAY_CAP:]

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
    overflow_members = sorted(nid for members in overflow for nid in members)
    others_members = sorted(overflow_members + loose_overflow_ids)
    if others_members:
        bubbles.append(_bubble(OTHERS_ID, "__others__", others_members))
    stats["bubble_count"] = len(bubbles)

    container_of: dict[str, str] = {}
    for bid, members in members_map.items():
        for m in members:
            container_of[m] = bid
    for hid in hub_ids:
        container_of[hid] = hid
    for nid in displayed_loose_ids:
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
        "loose": [by_id[nid] for nid in displayed_loose_ids],
        "edges": edges,
        "members": members_map,
        "stats": stats,
    }


def _tree_signature(workspace: Path) -> int:
    """Cheap stat-walk over everything the graph is built from.

    A stat per file (no reads, no parsing): any write bumps mtime_ns/size
    and misses the cache. Sessions are included because the full payload
    carries session nodes/edges even though structure ignores them.
    """
    items: list[tuple[str, int, int]] = []
    for class_name in ("entities", *_ENTRY_CLASSES):
        for p in walk_class(workspace, class_name):
            st = p.stat()
            items.append((str(p), st.st_mtime_ns, st.st_size))
    for extra_dir in (
        workspace / "memory" / "references",
        workspace / "sessions",
    ):
        if extra_dir.is_dir():
            for p in sorted(extra_dir.iterdir()):
                if p.is_file():
                    st = p.stat()
                    items.append((str(p), st.st_mtime_ns, st.st_size))
    return hash(tuple(sorted(items)))


def _put(
    cache: dict[Path, tuple[int, dict[str, Any]]],
    ws: Path,
    value: tuple[int, dict[str, Any]],
) -> None:
    """Insert/update under the caller's lock; evict oldest only on growth.

    Refreshing a key already in the cache must never evict anything else —
    eviction only makes room for a workspace the cache hasn't seen before.
    """
    if ws not in cache and len(cache) >= _CACHE_MAX:
        del cache[next(iter(cache))]
    cache[ws] = value


def _cached_payload(workspace: Path) -> tuple[int, dict[str, Any]]:
    """Tree signature and uncapped graph payload, rebuilt only on tree change.

    Synchronous and disk-heavy — event-loop callers must hop through
    ``asyncio.to_thread``.
    """
    ws = workspace.resolve()
    sig = _tree_signature(ws)
    with _lock:
        hit = _cache.get(ws)
        if hit is not None and hit[0] == sig:
            return sig, hit[1]
    payload = build_memory_graph(
        ws,
        max_nodes=_UNCAPPED_NODES,
        max_edges=_UNCAPPED_EDGES,
        include_sessions=True,
    )
    with _lock:
        _put(_cache, ws, (sig, payload))
    return sig, payload


def get_full_graph_cached(workspace: Path) -> dict[str, Any]:
    """Uncapped graph payload, rebuilt only when the memory tree changed."""
    return _cached_payload(workspace)[1]


def build_overview(workspace: Path) -> dict[str, Any]:
    """Overview payload for the workspace, cached at both levels.

    Both cache levels key on the same tree signature, so the overview cache
    invalidates exactly when the underlying payload would be rebuilt — no
    separate identity to fall out of sync with it.
    """
    ws = workspace.resolve()
    sig, payload = _cached_payload(ws)
    with _lock:
        hit = _overview_cache.get(ws)
        if hit is not None and hit[0] == sig:
            return hit[1]
    overview = assemble_overview(payload)
    with _lock:
        _put(_overview_cache, ws, (sig, overview))
    return overview


def _clear_all() -> None:
    """Reset both caches. Test isolation only."""
    with _lock:
        _cache.clear()
        _overview_cache.clear()


def build_cluster_subgraph(
    workspace: Path,
    ref: str,
    max_members: int = 150,
) -> dict[str, Any]:
    """Members-of-bubble subgraph for the neighborhood layer.

    Keyed by the bubble id (representative ref, or OTHERS_ID). Raises
    KeyError when the ref is not a bubble in the current overview — the
    caller maps that to a 404 and the client falls back to the overview.
    """
    ws = workspace.resolve()
    overview = build_overview(ws)
    members = overview.get("members", {}).get(ref)
    if members is None:
        raise KeyError(ref)
    payload = get_full_graph_cached(ws)
    by_id = {n["id"]: n for n in payload["nodes"]}
    member_set = set(members)
    kept_members = sorted(
        member_set,
        key=lambda m: (-int(by_id[m]["weight"]), m),
    )[:max_members]
    kept = set(kept_members)
    for e in payload["edges"]:
        a, b = e["source"], e["target"]
        if a in kept and b not in member_set and b in by_id:
            n = by_id[b]
            if n.get("phantom") or n["type"] == "session":
                kept.add(b)
        elif b in kept and a not in member_set and a in by_id:
            n = by_id[a]
            if n.get("phantom") or n["type"] == "session":
                kept.add(a)
    nodes = [by_id[nid] for nid in sorted(kept)]
    edges = [
        e
        for e in payload["edges"]
        if e["source"] in kept and e["target"] in kept
    ]
    return {
        "focus": ref,
        "nodes": nodes,
        "edges": edges,
        "total_members": len(member_set),
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "phantom_count": sum(1 for n in nodes if n.get("phantom")),
            "session_count": sum(1 for n in nodes if n["type"] == "session"),
            "truncated_nodes": len(member_set) > max_members,
            "truncated_edges": False,
            "types": sorted({n["type"] for n in nodes}),
        },
    }
