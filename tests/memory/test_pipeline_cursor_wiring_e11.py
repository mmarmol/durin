"""E11 (audit second pass, 2026-05-28): the v2 pipeline's
`_entity_aware_rerank` must pass `cursors` to
`entity_ranker.rank_with_entities` so the pre/post-cursor
partitioning documented in `docs/architecture/memory/03` §8.4 is actually
applied.

Pre-E11 the helper `_load_cursors_from_entities_dir` was orphaned
in `memory_search.py` and never wired into the v2 pipeline.
Consequence: pre-cursor episodic entries tagged with a query
entity received the same entity-match boost as post-cursor entries
— exactly the duplication §8.4 says to avoid.

This is a regression introduced silently when the v1 path was
removed in commit c820447 (Phase 5 d1 migration). The v1 path
passed `cursors=`; the v2 pipeline never did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage


def _seed_entity_page(workspace: Path, *, cursor: str) -> None:
    page = EntityPage(
        type="person",
        name="Marcelo",
        aliases=["m"],
        body="Builder.",
        dream_processed_through=cursor,
    )
    page.save(workspace / "memory" / "entities" / "person" / "marcelo.md")


def _seed_episodic(
    workspace: Path,
    *,
    name: str,
    valid_from: str,
    body: str,
    entities: list[str],
) -> Path:
    from durin.memory.store import store_memory

    return store_memory(
        workspace,
        content=body,
        class_name="episodic",
        entities=entities,
        valid_from=valid_from,
        slug=name,
    ).path


def test_pipeline_excludes_pre_cursor_from_entity_match_boost(
    tmp_path: Path,
) -> None:
    """The episodic pre-cursor entry (valid_from before the page's
    `dream_processed_through`) must NOT be boosted into the
    entity-match list. The post-cursor one MUST be boosted."""
    from durin.memory.search_pipeline import _entity_aware_rerank
    from durin.memory.rrf_fusion import FusedHit
    from durin.memory.aliases_cache import _clear_all

    _seed_entity_page(tmp_path, cursor="2026-02-15")

    # Two candidates: both tagged with `person:marcelo`. Pre-cursor
    # (2026-01-01) and post-cursor (2026-03-01).
    pre_uri = "episodic/2026/2026-01-10-pre"
    post_uri = "episodic/2026/2026-03-10-post"
    canonical_uri = "person:marcelo"

    fused = [
        FusedHit(uri=canonical_uri, score=0.5, sources={"vector"}, ranks={}),
        FusedHit(uri=pre_uri, score=0.9, sources={"vector"}, ranks={}),
        FusedHit(uri=post_uri, score=0.8, sources={"vector"}, ranks={}),
    ]
    vector_meta = {
        canonical_uri: {
            "uri": canonical_uri, "type": "entity",
            "entities": [canonical_uri], "valid_from": "",
        },
        pre_uri: {
            "uri": pre_uri, "type": "episodic",
            "entities": [canonical_uri],
            "valid_from": "2026-01-10",
        },
        post_uri: {
            "uri": post_uri, "type": "episodic",
            "entities": [canonical_uri],
            "valid_from": "2026-03-10",
        },
    }

    # _entity_aware_rerank loads the alias index from disk via
    # get_shared_alias_index. Clear any cached one from prior tests
    # so we get a fresh build from this tmp workspace.
    _clear_all()

    out = _entity_aware_rerank(
        tmp_path,
        "Marcelo",
        fused,
        vector_meta=vector_meta,
        lexical_meta={},
        grep_meta={},
    )

    # The canonical page is always in the entity-match list (head).
    # Post-cursor entries are also in the list. Pre-cursor entries
    # are excluded from the list — they may still appear later via
    # base vector ranking, but not boosted to the head.
    uris_in_order = [h.uri for h in out]
    canonical_pos = uris_in_order.index(canonical_uri)
    post_pos = uris_in_order.index(post_uri)
    pre_pos = uris_in_order.index(pre_uri)

    # Pre-cursor must NOT be ahead of post-cursor. Without cursor
    # wiring, both pre and post are boosted indiscriminately and
    # the pre with score 0.9 lands above the post with 0.8.
    assert post_pos < pre_pos, (
        f"post-cursor must outrank pre-cursor in entity-match list; "
        f"got order {uris_in_order}"
    )
    # And the canonical leads the entity-match cluster.
    assert canonical_pos == 0


def test_pipeline_cursors_loaded_from_entity_pages(
    tmp_path: Path,
) -> None:
    """The cursor map must come from each entity page's
    `dream_processed_through` field; a page without a cursor yields
    no entry (which means rank_with_entities treats all matches as
    post-cursor — backward-compatible)."""
    from durin.memory.entity_ranker import load_cursors_from_entities_dir

    _seed_entity_page(tmp_path, cursor="2026-02-15")
    EntityPage(
        type="org", name="Acme", aliases=[], body="Company.",
    ).save(tmp_path / "memory" / "entities" / "org" / "acme.md")

    cursors = load_cursors_from_entities_dir(
        tmp_path / "memory",
        ["person:marcelo", "org:acme", "person:nonexistent"],
    )
    assert cursors == {"person:marcelo": "2026-02-15"}
