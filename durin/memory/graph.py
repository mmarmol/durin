"""Entity-centric memory graph builder for the webui Obsidian-style view.

Produces a JSON-serialisable ``{"nodes": [...], "edges": [...]}`` shape
the frontend force-directed canvas renders. Read-only over the on-disk
state — no LLM call, no mutation.

**Nodes** are entity pages under ``memory/entities/<type>/<slug>.md``
(excluding ``archive/`` subfolders since those are absorbed-and-de-
indexed by design). Each carries the entity ref, display name, type,
aliases, and a ``weight`` proportional to how many episodic entries
mention it (size hint for the renderer).

**Edges** come from episodic-entry co-occurrence: every entry that
tags ≥2 entities contributes a +1 weight to each unordered pair. The
result is an undirected weighted graph where stronger ties mean "these
two entities appear together more often in raw memory".

Future evolutions (kept as comments in the code):

- Edges from entity-page body cross-references (when the consolidator
  emits explicit ``[other-ref]`` markdown links — not in V1 prompt).
- Edges from absorption history (archived → canonical chain) so
  drill-down can show the merge ancestry visually.
- Edges from same-session co-occurrence (entries written in the same
  session.jsonl, regardless of entity tag overlap).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

from durin.memory.entity_page import EntityPage
from durin.memory.storage import load_entry

__all__ = ["build_memory_graph"]

logger = logging.getLogger(__name__)


def build_memory_graph(
    workspace: Path,
    *,
    max_nodes: int = 500,
    max_edges: int = 2000,
) -> dict[str, Any]:
    """Return ``{"nodes": [...], "edges": [...], "stats": {...}}``.

    Walks the on-disk memory tree once for pages, once for episodic
    entries. Caps node + edge counts so a runaway workspace doesn't
    ship a 50 MB JSON payload over the websocket channel — callers
    can request finer-grained slices later if needed.
    """
    memory_root = Path(workspace) / "memory"
    entities_root = memory_root / "entities"
    episodic_root = memory_root / "episodic"

    # 1. Walk entity pages (skip archived).
    nodes_by_ref: dict[str, dict[str, Any]] = {}
    if entities_root.is_dir():
        for type_dir in sorted(entities_root.iterdir()):
            if not type_dir.is_dir():
                continue
            type_name = type_dir.name
            for page_path in sorted(type_dir.glob("*.md")):
                # Skip pages under <slug>/archive/<absorbed>.md — those
                # are intentionally de-indexed by EntityAbsorption.
                if "archive" in page_path.relative_to(entities_root).parts:
                    continue
                try:
                    page = EntityPage.from_file(page_path)
                except Exception:  # noqa: BLE001
                    continue
                if page is None:
                    continue
                slug = page_path.stem
                ref = f"{type_name}:{slug}"
                nodes_by_ref[ref] = {
                    "id": ref,
                    "type": type_name,
                    "name": page.name or slug,
                    "aliases": list(page.aliases or []),
                    "weight": 0,  # filled from episodic count below
                }

    # 2. Walk episodic entries: accumulate per-ref entry count + pairwise
    # co-occurrence counts. Skip refs not present as an entity page (the
    # entry tagged a type:value that nobody has consolidated yet — show
    # those as "phantom" nodes so the user sees coverage gaps).
    edge_counts: dict[tuple[str, str], int] = defaultdict(int)
    phantom_refs: dict[str, int] = defaultdict(int)
    if episodic_root.is_dir():
        for entry_path in episodic_root.glob("*.md"):
            try:
                entry = load_entry(entry_path)
            except Exception:  # noqa: BLE001
                continue
            refs = sorted({r for r in (entry.entities or []) if ":" in r})
            for ref in refs:
                node = nodes_by_ref.get(ref)
                if node is not None:
                    node["weight"] += 1
                else:
                    phantom_refs[ref] += 1
            # Co-occurrence: every pair within this entry gets +1.
            for a, b in combinations(refs, 2):
                key = (a, b) if a < b else (b, a)
                edge_counts[key] += 1

    # 3. Phantom nodes — entity refs tagged in entries but with no
    # consolidated page. Render them with a flag so the frontend can
    # style differently (e.g. dashed border).
    for ref, count in phantom_refs.items():
        if ref in nodes_by_ref:
            continue
        type_name, _, slug = ref.partition(":")
        nodes_by_ref[ref] = {
            "id": ref,
            "type": type_name or "unknown",
            "name": slug or ref,
            "aliases": [],
            "weight": count,
            "phantom": True,
        }

    # 4. Build the edge list. Only keep edges where both endpoints are
    # in the node set (defensive; same-ref edges already collapsed
    # by the sorted() dedup above).
    edges: list[dict[str, Any]] = []
    for (a, b), weight in edge_counts.items():
        if a in nodes_by_ref and b in nodes_by_ref:
            edges.append({"source": a, "target": b, "weight": weight})

    # 5. Cap: prefer higher-weight nodes/edges, drop the tail.
    nodes = sorted(
        nodes_by_ref.values(),
        key=lambda n: (-int(n["weight"]), n["id"]),
    )
    truncated_nodes = len(nodes) > max_nodes
    nodes = nodes[:max_nodes]
    kept_ref_set = {n["id"] for n in nodes}

    edges = [e for e in edges if e["source"] in kept_ref_set and e["target"] in kept_ref_set]
    edges.sort(key=lambda e: (-int(e["weight"]), e["source"], e["target"]))
    truncated_edges = len(edges) > max_edges
    edges = edges[:max_edges]

    # 6. Type palette hint for the frontend — stable order so the
    # legend doesn't reshuffle every payload.
    types_seen = sorted({n["type"] for n in nodes})

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "phantom_count": sum(1 for n in nodes if n.get("phantom")),
            "truncated_nodes": truncated_nodes,
            "truncated_edges": truncated_edges,
            "types": types_seen,
        },
    }
