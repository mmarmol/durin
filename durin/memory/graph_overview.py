"""Overview builder: cluster bubbles + semantic hubs for the memory graph.

Clustering topology is computed from SEMANTIC evidence only: entity-entity
co-mention edges and typed relations between consolidated pages (plus
reference ``derived_from`` links). Sessions and phantoms never enter
clustering input — a session edges into everything it touched, and with
few sessions the "communities" would degenerate into "what session A
touched" rather than a real topic cluster. Session evidence does feed the
per-entity importance score (see ``_entity_scores``) as a scalar on top of
a node already placed by semantic structure — real workspaces carry no
other signal of what matters — but it never becomes an edge between nodes.
A node qualifies as a hub only when its score is a clear outlier — at
least twice the median score among entities — so uniform or young graphs
surface no hubs at all, and the qualifying count is always capped at
HUB_COUNT.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from durin.memory.graph import build_memory_graph
from durin.memory.paths import walk_class

HUB_COUNT = 20
BUBBLE_MIN_MEMBERS = 8
BUBBLE_DISPLAY_CAP = 30
LOOSE_DISPLAY_CAP = 30
# Damping on distinct-session evidence in the importance score (see
# `_entity_scores`) — keeps high-churn operational entities from drowning
# the semantic structure as session volume grows.
SESSION_LOG_FACTOR = 2.0

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
    scores: dict[str, float],
    top_n: int = HUB_COUNT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Top entities by importance score, removed before clustering.

    Mega-connectors bridge every community and would merge them into one
    blob, so they are drawn individually instead. References hold real
    content but are consultation material, not connectors — never hubs.
    """
    eligible = sorted(
        (n for n in nodes if n["type"] != "reference"),
        key=lambda n: (-scores[n["id"]], n["id"]),
    )
    hubs = eligible[:top_n]
    hub_ids = {n["id"] for n in hubs}
    rest = [n for n in nodes if n["id"] not in hub_ids]
    return hubs, rest


def _entity_scores(
    payload: dict[str, Any],
    sem_nodes: list[dict[str, Any]],
    sem_edges: list[dict[str, Any]],
) -> dict[str, float]:
    """Overview importance score per semantic node.

    Real workspaces carry no per-entry mention counts (the legacy entry
    classes are empty), so raw ``weight`` is a dead signal. Importance is
    instead: relation/derived degree — dream's deliberate structure — plus
    a log-damped count of distinct sessions whose evidence touched the
    node. The damping keeps high-churn operational entities (support
    tickets, test cases) from drowning the semantic structure as session
    volume grows.
    """
    degree: dict[str, int] = defaultdict(int)
    for e in sem_edges:
        degree[e["source"]] += 1
        degree[e["target"]] += 1
    session_touches: dict[str, int] = defaultdict(int)
    for e in payload["edges"]:
        source, target = e["source"], e["target"]
        source_is_session = source.startswith("session:")
        target_is_session = target.startswith("session:")
        if source_is_session and not target_is_session:
            session_touches[target] += 1
        elif target_is_session and not source_is_session:
            session_touches[source] += 1
    return {
        n["id"]: degree.get(n["id"], 0)
        + SESSION_LOG_FACTOR * math.log1p(session_touches.get(n["id"], 0))
        for n in sem_nodes
    }


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
    when its importance score (see ``_entity_scores``) is at least twice
    the median score among entities (floor 3.0), and the qualifying count
    is capped at HUB_COUNT — so uniform or young graphs extract no hubs at
    all. Label propagation then runs on the rest; communities >=
    BUBBLE_MIN_MEMBERS become bubbles keyed by their highest-scoring
    member; the rest stay as loose nodes, display-capped at
    LOOSE_DISPLAY_CAP by score, with any overflow folded into the same
    others bubble that absorbs bubble overflow. Edges are summed between
    containers. mode="flat" when no community reaches the threshold — small
    or young workspaces render the plain graph instead, though hubs (found
    before clustering is attempted) still populate when present.
    """
    sem_nodes, sem_edges = _semantic_view(payload)
    by_id = {n["id"]: n for n in sem_nodes}
    scores = _entity_scores(payload, sem_nodes, sem_edges)

    # Hub qualification: a hub is an entity clearly higher-scoring than the
    # typical node — at least twice the median score (and never below 3).
    # Uniform or young graphs therefore extract no hubs at all (nothing is
    # an outlier), and the count is hard-capped so the overview stays small.
    non_ref_nodes = [n for n in sem_nodes if n["type"] != "reference"]
    if non_ref_nodes:
        node_scores = sorted(scores[n["id"]] for n in non_ref_nodes)
        median = node_scores[len(node_scores) // 2]
        floor = max(3.0, 2 * median)
        qualifying = sum(1 for n in non_ref_nodes if scores[n["id"]] >= floor)
        hubs, rest = _extract_hubs(sem_nodes, scores, top_n=min(HUB_COUNT, qualifying))
    else:
        hubs, rest = [], sem_nodes
    hub_ids = {n["id"] for n in hubs}
    # Copies, not mutation: `payload` (and therefore `sem_nodes`) may be a
    # cached object shared across calls — the emitted "weight" carries the
    # score, but the cached node dict's own weight field is left untouched.
    hubs_out = [{**n, "weight": round(scores[n["id"]], 1)} for n in hubs]
    rest_ids = [n["id"] for n in rest]
    rest_edges = [
        e
        for e in sem_edges
        if e["source"] not in hub_ids and e["target"] not in hub_ids
    ]
    labels = _label_propagation(rest_ids, _build_adjacency(rest_edges))
    comms = _communities(labels)

    def _rep(members: list[str]) -> str:
        return min(members, key=lambda m: (-scores[m], m))

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
            "hubs": hubs_out,
            "loose": [],
            "edges": [],
            "members": {},
            "stats": stats,
        }

    loose_by_score = sorted(loose_ids, key=lambda nid: (-scores[nid], nid))
    displayed_loose_ids = loose_by_score[:LOOSE_DISPLAY_CAP]
    loose_overflow_ids = loose_by_score[LOOSE_DISPLAY_CAP:]

    shown, overflow = sized[:BUBBLE_DISPLAY_CAP], sized[BUBBLE_DISPLAY_CAP:]
    members_map: dict[str, list[str]] = {}
    bubbles: list[dict[str, Any]] = []

    def _bubble(bid: str, name: str, members: list[str]) -> dict[str, Any]:
        top = sorted(members, key=lambda m: (-scores[m], m))[:5]
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
                    "weight": round(scores[m], 1),
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
        "hubs": hubs_out,
        "loose": [
            {**by_id[nid], "weight": round(scores[nid], 1)}
            for nid in displayed_loose_ids
        ],
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
    """Uncapped graph payload, rebuilt only when the memory tree changed.

    Synchronous and disk-heavy — event-loop callers hop through `asyncio.to_thread`.
    """
    return _cached_payload(workspace)[1]


def _overview_from(ws: Path, sig: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Overview for an already-fetched (signature, payload) snapshot.

    Split out of `build_overview` so a caller that also needs the payload
    itself (`build_cluster_subgraph`) can fetch it once via `_cached_payload`
    and hand both pieces here, instead of calling `build_overview` and
    `get_full_graph_cached` separately — two independent stat-walks that a
    concurrent write could land between, desyncing the overview's members
    from the payload's node lookup.

    Both cache levels key on the same tree signature, so the overview cache
    invalidates exactly when the underlying payload would be rebuilt — no
    separate identity to fall out of sync with it.
    """
    with _lock:
        hit = _overview_cache.get(ws)
        if hit is not None and hit[0] == sig:
            return hit[1]
    overview = assemble_overview(payload)
    with _lock:
        _put(_overview_cache, ws, (sig, overview))
    return overview


def build_overview(workspace: Path) -> dict[str, Any]:
    """Overview payload for the workspace, cached at both levels.

    Synchronous and disk-heavy — event-loop callers hop through `asyncio.to_thread`.
    """
    ws = workspace.resolve()
    sig, payload = _cached_payload(ws)
    return _overview_from(ws, sig, payload)


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
    KeyError when ``ref`` is not a bubble in the current overview — the
    caller maps that to a 404 and the client falls back to the overview.

    Members are capped at ``max_members``, kept by descending importance
    score (see ``_entity_scores``) with a deterministic tie-break on id;
    ``total_members`` always reports the full pre-cap member count, capped
    or not. Phantom entities and session nodes count as scaffolding —
    context around the members, toggleable in the client — when they sit
    directly on an edge to a KEPT member; that check runs against the
    capped member set only, so a scaffolding node can never itself unlock
    a second one purely by association — inclusion never cascades past
    one hop from a real member.

    Overview and payload come from one shared (signature, payload)
    snapshot — a single tree stat-walk — so a member can never point at a
    node id the payload doesn't have.

    Synchronous and disk-heavy — event-loop callers hop through `asyncio.to_thread`.
    """
    ws = workspace.resolve()
    sig, payload = _cached_payload(ws)
    overview = _overview_from(ws, sig, payload)
    members = overview.get("members", {}).get(ref)
    if members is None:
        raise KeyError(ref)
    by_id = {n["id"]: n for n in payload["nodes"]}
    sem_nodes, sem_edges = _semantic_view(payload)
    scores = _entity_scores(payload, sem_nodes, sem_edges)
    member_set = set(members)
    kept_members = sorted(
        member_set,
        key=lambda m: (-scores[m], m),
    )[:max_members]
    kept_member_ids = set(kept_members)
    scaffold: set[str] = set()
    for e in payload["edges"]:
        a, b = e["source"], e["target"]
        if a in kept_member_ids and b not in member_set and b in by_id:
            n = by_id[b]
            if n.get("phantom") or n["type"] == "session":
                scaffold.add(b)
        elif b in kept_member_ids and a not in member_set and a in by_id:
            n = by_id[a]
            if n.get("phantom") or n["type"] == "session":
                scaffold.add(a)
    kept = kept_member_ids | scaffold
    nodes = [
        {**by_id[nid], "weight": round(scores[nid], 1)}
        if nid in kept_member_ids
        else by_id[nid]
        for nid in sorted(kept)
    ]
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
