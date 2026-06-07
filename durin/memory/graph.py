"""Entity-centric memory graph builder for the webui Obsidian-style view.

Produces a JSON-serialisable ``{"nodes": [...], "edges": [...]}`` shape
the frontend force-directed canvas renders. Read-only over the on-disk
state — no LLM call, no mutation.

**Node kinds**:

- *Entity pages* under ``memory/entities/<type>/<slug>.md`` (excluding
  ``archive/`` subfolders since those are absorbed-and-de-indexed by
  design). Carry the entity ref, display name, type, aliases.
- *Phantom entities* — refs that appear in entries but have no
  consolidated page yet. Rendered with a flag so the frontend can
  style differently (dashed border).
- *Sessions* (optional, default ON) — one per ``sessions/<key>.jsonl``.
  Type ``"session"``. Lets the user see conversation → entity flow
  alongside entity ↔ entity flow.

**Edges**:

- *Entity ↔ entity*: entry co-occurrence across the ``episodic``,
  ``stable`` and ``corpus`` classes — every entry that tags ≥2
  entities contributes +1 to each unordered pair.
- *Session → entity*: derived from session ``meta.json::derived._last_tags``
  AND from episodic-entry ``source_refs`` that link back to
  ``sessions/<key>.md``. Weight = count of evidence per (session, ref).

Future evolutions:

- Edges from entity-page body cross-references (when the consolidator
  emits explicit ``[other-ref]`` markdown links).
- Edges from absorption history (archived → canonical chain).
- Session ↔ session edges via shared entities (deferred — risk noisy).
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from itertools import chain, combinations
from pathlib import Path
from typing import Any

from durin.memory.entity_page import EntityPage
from durin.memory.storage import load_entry

__all__ = ["build_memory_graph", "build_entity_subgraph"]

logger = logging.getLogger(__name__)

_SESSION_REF_RE = re.compile(r"sessions/([^/.#]+)\.md")

# Memory classes whose entries carry `entities` tags and therefore feed
# the graph's nodes/edges. `pending` (intake buffer) and `session_summary`
# are excluded: the former is not user-visible, the latter is already
# represented by the dedicated session nodes.
_ENTRY_CLASSES: tuple[str, ...] = ("episodic", "stable", "corpus")


def build_memory_graph(
    workspace: Path,
    *,
    max_nodes: int = 500,
    max_edges: int = 2000,
    include_sessions: bool = True,
) -> dict[str, Any]:
    """Return ``{"nodes": [...], "edges": [...], "stats": {...}}``.

    Walks the on-disk memory tree once for entity pages, once for the
    entry classes that carry entity tags (``episodic``, ``stable``,
    ``corpus``); optionally walks ``sessions/`` and links each session to
    the entities it tagged. Caps node + edge counts so a runaway
    workspace doesn't ship a 50 MB JSON payload — callers can request
    finer-grained slices later if needed.

    ``include_sessions=False`` skips the sessions/ walk entirely —
    useful for tests that want to assert the entity-only invariants
    or for callers that already know they don't have a sessions/ tree.
    """
    workspace = Path(workspace)
    memory_root = workspace / "memory"
    entities_root = memory_root / "entities"
    sessions_root = workspace / "sessions"

    # 1. Walk entity pages — `walk_class` excludes the top-level
    # archive folder by default (Phase 0 deliverables 1 + 5).
    from durin.memory.paths import walk_class

    nodes_by_ref: dict[str, dict[str, Any]] = {}
    # G1: explicit entity-page relations become typed edges (source, target,
    # type). Previously the graph only drew co-occurrence edges from entry tags,
    # so the new model's `relations` field rendered as 0 edges.
    relation_edges: list[tuple[str, str, str]] = []
    for page_path in walk_class(workspace, "entities"):
        # `entities/<type>/<slug>.md` — derive `<type>` from the path.
        rel = page_path.relative_to(entities_root)
        if len(rel.parts) < 2:
            continue
        type_name = rel.parts[0]
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
        for rel in (page.relations or []):
            to_ref = str(rel.get("to") or "")
            if ":" in to_ref:
                relation_edges.append(
                    (ref, to_ref, str(rel.get("type") or "related"))
                )

    # 2. Walk entry classes (episodic + stable + corpus): accumulate
    # per-ref entry count + pairwise co-occurrence counts. Skip refs not
    # present as an entity page (the entry tagged a type:value that nobody
    # has consolidated yet — show those as "phantom" nodes so the user
    # sees coverage gaps).
    # Also harvest session refs from each entry's source_refs so we can
    # later draw session→entity edges from "this entry was authored
    # during conversation X" evidence.
    edge_counts: dict[tuple[str, str], int] = defaultdict(int)
    phantom_refs: dict[str, int] = defaultdict(int)
    # session_entity_evidence[session_ref][entity_ref] = count of entries
    # that tag entity_ref AND were sourced from session_ref. Used to
    # build weighted session→entity edges.
    session_entity_evidence: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int),
    )
    entry_paths = chain.from_iterable(
        walk_class(workspace, class_name) for class_name in _ENTRY_CLASSES
    )
    for entry_path in entry_paths:
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
        # Session evidence: parse source_refs for sessions/<key>.md
        # (doc 18 §5.3 link format). Tracks per-(session,entity)
        # co-mentions so the resulting edge weight is meaningful.
        if include_sessions:
            for src in (entry.source_refs or []):
                m = _SESSION_REF_RE.search(str(src))
                if m is None:
                    continue
                sess_ref = f"session:{m.group(1)}"
                for ref in refs:
                    session_entity_evidence[sess_ref][ref] += 1

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

    # 3.5 Session nodes + meta-derived entity links (optional). One
    # node per <key>.jsonl, weighted by message count. Edges fold in:
    # (a) the per-entry source_refs evidence collected in step 2, and
    # (b) any meta.json::derived._last_tags.entities lists (entities
    # the consolidator surfaced as "this session was about X").
    if include_sessions and sessions_root.is_dir():
        for jsonl_path in sorted(sessions_root.glob("*.jsonl")):
            stem = jsonl_path.stem
            sess_ref = f"session:{stem}"
            title, message_count = _read_session_summary(jsonl_path)
            nodes_by_ref[sess_ref] = {
                "id": sess_ref,
                "type": "session",
                "name": title or stem,
                "aliases": [],
                "weight": message_count,
            }
            # Add meta-driven evidence (additive on top of source_refs).
            meta_tags = _read_session_meta_entities(
                sessions_root / f"{stem}.meta.json"
            )
            if meta_tags:
                bucket = session_entity_evidence.setdefault(sess_ref, defaultdict(int))
                for tag in meta_tags:
                    bucket[tag] += 1

        # Promote any meta-only refs into phantom nodes so the
        # corresponding session→entity edge has both endpoints
        # registered. A meta tag for an entity that NO episodic entry
        # has ever mentioned is still useful signal worth showing.
        for ents in session_entity_evidence.values():
            for ref in ents.keys():
                if ref in nodes_by_ref:
                    continue
                type_name, _, slug = ref.partition(":")
                nodes_by_ref[ref] = {
                    "id": ref,
                    "type": type_name or "unknown",
                    "name": slug or ref,
                    "aliases": [],
                    "weight": 0,
                    "phantom": True,
                }

    # 3.6 (G1 / policy-a): register a page-less relation target as a phantom
    # node only when >=2 distinct sources point at it. A dangling relation to
    # a target nobody else references is a degree-1 leaf that adds no graph
    # structure — the relation still lives on disk in the source page's
    # frontmatter (searchable), but we don't draw an empty node for it. The
    # target is promoted to a real hub once a second entity relates to it (or
    # once it gets its own consolidated page). Its edge is dropped downstream
    # by the both-endpoints-present guard when the node is absent.
    rel_target_sources: dict[str, set[str]] = defaultdict(set)
    for src, to_ref, _t in relation_edges:
        rel_target_sources[to_ref].add(src)
    for to_ref, sources in rel_target_sources.items():
        if to_ref in nodes_by_ref or len(sources) < 2:
            continue
        t_type, _, t_slug = to_ref.partition(":")
        nodes_by_ref[to_ref] = {
            "id": to_ref,
            "type": t_type or "unknown",
            "name": t_slug or to_ref,
            "aliases": [],
            "weight": 0,
            "phantom": True,
        }

    # 4. Build the edge list. Only keep edges where both endpoints are
    # in the node set (defensive; same-ref edges already collapsed
    # by the sorted() dedup above).
    edges: list[dict[str, Any]] = []
    for (a, b), weight in edge_counts.items():
        if a in nodes_by_ref and b in nodes_by_ref:
            edges.append({"source": a, "target": b, "weight": weight})

    # G1: explicit entity-page relations as typed edges (carry the relation
    # type so the webui can label "founded_by", "partner", …).
    for src_ref, to_ref, rtype in relation_edges:
        if src_ref in nodes_by_ref and to_ref in nodes_by_ref:
            edges.append({
                "source": src_ref,
                "target": to_ref,
                "type": rtype,
                "kind": "relation",
                "weight": 1,
            })

    # Session→entity edges. We keep them DIRECTIONAL conceptually
    # (a session links to the entities it discussed) but the JSON
    # shape stays the same — the renderer treats them as undirected
    # like the others. Phantom-target edges are allowed: a session
    # surfaced an entity that no one consolidated yet, that's still
    # a real co-mention worth showing.
    if include_sessions:
        for sess_ref, ents in session_entity_evidence.items():
            if sess_ref not in nodes_by_ref:
                # Session walked off the disk between steps; skip.
                continue
            for ent_ref, w in ents.items():
                if ent_ref not in nodes_by_ref:
                    continue
                edges.append({"source": sess_ref, "target": ent_ref, "weight": w})

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
    session_count = sum(1 for n in nodes if n["type"] == "session")

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "phantom_count": sum(1 for n in nodes if n.get("phantom")),
            "session_count": session_count,
            "truncated_nodes": truncated_nodes,
            "truncated_edges": truncated_edges,
            "types": types_seen,
        },
    }


def build_entity_subgraph(
    workspace: Path,
    ref: str,
    *,
    hops: int = 1,
    max_neighbours: int = 150,
) -> dict[str, Any]:
    """Ego-graph for one node: ``ref`` + everything within ``hops`` edges.

    Powers the webui's "focus" mode (Obsidian's local graph). The whole
    point is that it is NOT subject to the global node cap — a node the
    overview dropped (or that the user reached via search) is always
    present here, centred, with just its neighbourhood around it.

    Built by walking the full graph uncapped and keeping the BFS closure
    of ``ref`` out to ``hops``. If ``ref`` has no drawn edges (e.g. an
    isolated entity, or one whose only relations were degree-1 phantoms
    suppressed by policy), the result is the single node — correct: it
    genuinely connects to nothing yet. When ``ref`` isn't a real node at
    all, a synthetic placeholder is returned so the panel still opens.
    """
    full = build_memory_graph(
        workspace, max_nodes=100_000, max_edges=400_000,
    )
    nodes_by_id: dict[str, dict[str, Any]] = {n["id"]: n for n in full["nodes"]}
    edges = full["edges"]

    keep: set[str] = {ref}
    frontier: set[str] = {ref}
    for _ in range(max(1, hops)):
        nxt: set[str] = set()
        for e in edges:
            s, t = e["source"], e["target"]
            if s in frontier and t not in keep:
                nxt.add(t)
            elif t in frontier and s not in keep:
                nxt.add(s)
        if not nxt:
            break
        keep |= nxt
        frontier = nxt

    # Cap the neighbourhood (keep ref + highest-weight neighbours) so a
    # mega-hub doesn't ship its whole 1-hop fan-out.
    if len(keep) > max_neighbours + 1:
        ranked = sorted(
            (i for i in keep if i != ref),
            key=lambda i: -int(nodes_by_id.get(i, {}).get("weight", 0)),
        )
        keep = {ref, *ranked[:max_neighbours]}

    nodes = [nodes_by_id[i] for i in keep if i in nodes_by_id]
    if ref not in nodes_by_id:
        # ref isn't a drawn node (capped out with no edges, or pure
        # placeholder) — synthesise it so focus still has a centre.
        type_name, _, slug = ref.partition(":")
        nodes.append({
            "id": ref,
            "type": type_name or "unknown",
            "name": slug or ref,
            "aliases": [],
            "weight": 0,
        })
    sub_edges = [
        e for e in edges if e["source"] in keep and e["target"] in keep
    ]
    types_seen = sorted({n["type"] for n in nodes})
    return {
        "nodes": nodes,
        "edges": sub_edges,
        "focus": ref,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(sub_edges),
            "phantom_count": sum(1 for n in nodes if n.get("phantom")),
            "session_count": sum(1 for n in nodes if n["type"] == "session"),
            "truncated_nodes": False,
            "truncated_edges": False,
            "types": types_seen,
        },
    }


_FIRST_USER_PREVIEW_MAX = 48


def _read_session_summary(jsonl_path: Path) -> tuple[str | None, int]:
    """Return (display_name, message_count) without parsing the full file.

    Line 0 is the identity block (title, channel, …); subsequent lines
    are messages. We tolerate truncated / malformed files by counting
    lines that look like JSON objects.

    Display-name resolution order, falling through on each miss:

    1. ``metadata.title`` from the identity block — the webui title
       set by :func:`maybe_generate_webui_title` (LLM-generated) or by
       the user via the P2 rename endpoint. This is the authoritative
       source when present.
    2. Legacy top-level keys ``display_name`` / ``title`` / ``name``
       — older sessions or non-webui channels may stash a name here.
    3. First user message excerpt — same fallback the sidebar uses via
       ``preview``. Keeps the graph nodes aligned with what the user
       sees in the chat list when LLM auto-titling hasn't run yet.
    4. Channel prefix + short UUID suffix derived from the file stem.
       ``websocket_12c54195-1548-…`` → ``ws · 12c54195``,
       ``cli_direct`` → ``cli · direct``.
    """
    title: str | None = None
    first_user_preview: str | None = None
    count = 0
    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for i, raw in enumerate(fh):
                raw = raw.strip()
                if not raw:
                    continue
                line_obj: Any = None
                try:
                    line_obj = json.loads(raw)
                except json.JSONDecodeError:
                    line_obj = None

                if i == 0:
                    if isinstance(line_obj, dict):
                        # 1. metadata.title (webui-managed)
                        metadata = line_obj.get("metadata")
                        if isinstance(metadata, dict):
                            candidate = metadata.get("title")
                            if isinstance(candidate, str) and candidate.strip():
                                title = candidate.strip()
                        # 2. Legacy fallbacks
                        if not title:
                            title = (
                                line_obj.get("display_name")
                                or line_obj.get("title")
                                or line_obj.get("name")
                            )
                        # If the first line is itself a message (some
                        # older sessions skip the identity block), count
                        # it too — and treat it as a candidate preview.
                        if "role" in line_obj:
                            count += 1
                            if (
                                first_user_preview is None
                                and line_obj.get("role") == "user"
                                and isinstance(line_obj.get("content"), str)
                            ):
                                first_user_preview = line_obj["content"].strip()
                    continue

                count += 1
                # 3. Capture the first user message we see — mirrors
                # ``preview`` shown in the sidebar.
                if (
                    title is None
                    and first_user_preview is None
                    and isinstance(line_obj, dict)
                    and line_obj.get("role") == "user"
                    and isinstance(line_obj.get("content"), str)
                ):
                    first_user_preview = line_obj["content"].strip()
    except OSError:
        return None, 0
    if not title and first_user_preview:
        # Clip the preview so very long first messages don't blow out
        # the graph node label width.
        text = first_user_preview
        if len(text) > _FIRST_USER_PREVIEW_MAX:
            text = text[: _FIRST_USER_PREVIEW_MAX - 1].rstrip() + "…"
        title = text
    if not title:
        title = _friendly_session_label(jsonl_path.stem)
    return title, count


_CHANNEL_ABBREV = {
    "websocket": "ws",
    "cli": "cli",
    "telegram": "tg",
    "slack": "slack",
    "discord": "dc",
    "matrix": "matrix",
    "whatsapp": "wa",
    "feishu": "feishu",
    "dingtalk": "dt",
    "mochat": "mochat",
    "qq": "qq",
    "wecom": "wc",
}


def _friendly_session_label(stem: str) -> str:
    """Render a compact human label from a session filename stem.

    ``websocket_12c54195-1548-4d76-925f-dc772b023f40`` → ``ws · 12c54195``
    ``cli_direct`` → ``cli · direct``
    ``standalone_abc``  → ``standalone_abc`` (unknown prefix, returned as-is)
    """
    if "_" not in stem:
        return stem
    prefix, _, rest = stem.partition("_")
    if not rest:
        return stem
    abbrev = _CHANNEL_ABBREV.get(prefix.lower())
    if abbrev is None:
        # Unknown prefix — surface the stem so the user has full
        # context; the frontend truncation will handle visual length.
        return stem
    # For UUID-like rest (8+ hex chars before a dash), keep just the
    # leading chunk. For short non-UUID rests like ``direct`` or
    # ``chat42``, keep the whole thing — it's already readable.
    first_hex = rest.split("-", 1)[0]
    if "-" in rest and len(first_hex) >= 8 and all(
        c in "0123456789abcdef" for c in first_hex.lower()
    ):
        suffix = first_hex
    else:
        suffix = rest
    return f"{abbrev} · {suffix}"


def _read_session_meta_entities(meta_path: Path) -> list[str]:
    """Extract entity refs from ``meta.json::derived._last_tags.entities``.

    Tolerates partial / missing structure: this field is best-effort
    populated by the curator and may not exist for every session.
    """
    if not meta_path.is_file():
        return []
    try:
        with meta_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    derived = data.get("derived") if isinstance(data, dict) else None
    if not isinstance(derived, dict):
        return []
    tags = derived.get("_last_tags") if isinstance(derived, dict) else None
    if not isinstance(tags, dict):
        return []
    entities = tags.get("entities")
    if not isinstance(entities, list):
        return []
    return [str(e) for e in entities if isinstance(e, str) and ":" in e]
