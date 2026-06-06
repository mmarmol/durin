"""End-to-end Phase 3 L1 light retrieval test.

Wires together (without live LLM):
- Phase 1.1 typed entities tagging in memory entries
- Phase 1.4 alias_index populated from a consolidated entity page
- Phase 3.0 entity page indexed in vector_index
- Phase 3.1 query → entity extraction via alias_index
- Phase 3.2/3.3 multi-factor ranking applied to vector results

Validates the integration assertion of doc 18 §7 / doc 19 §5: when a
query mentions an entity by name/alias/identifier, the canonical page
+ post-cursor entries surface in the top results, and pre-cursor
entries are demoted (info is consolidated).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable

import pytest

from durin.memory.aliases_index import AliasIndex
from durin.memory.embedding import EmbeddingProvider
from durin.memory.entity_page import EntityPage
from durin.memory.entity_ranker import (
    extract_query_entities,
    rank_with_entities,
)
from durin.memory.store import store_memory
from durin.memory.storage import load_entry
from durin.memory.vector_index import VectorIndex, vector_index_available


pytestmark = pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb is not installed; install durin[memory] to run",
)


class _DeterministicProvider(EmbeddingProvider):
    """Fake provider that gives identical/similar vectors to related texts.

    Strategy: lowercase + remove punctuation, then hash characters into
    a small vector. Two texts that share many characters get close
    vectors. Real fastembed is overkill for an integration shape check.
    """

    DIM = 16

    @property
    def model_name(self) -> str:
        return "fake/deterministic"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.DIM
            cleaned = text.lower()
            for ch in cleaned:
                if ch.isalpha():
                    idx = (ord(ch) - ord("a")) % self.DIM
                    vec[idx] += 1.0
            out.append(vec)
        return out


@pytest.fixture
def provider() -> _DeterministicProvider:
    return _DeterministicProvider()


def test_retrieval_l1_light_end_to_end(
    tmp_path: Path, provider: _DeterministicProvider
) -> None:
    """Full L1 light retrieval pipeline against a small fixture corpus.

    Setup:
    - 3 memory entries about person:marcelo
        - pre-cursor (consolidated already, should demote)
        - post-cursor (fresh, should boost)
        - unrelated (no entity tag, baseline)
    - 1 consolidated entity page for person:marcelo
    - alias_index populated from the page
    - vector_index has all 3 entries + the page

    Query: "what does Marcelo prefer about testing?"

    Expected: page + post-cursor entry rank in top; pre-cursor at bottom.
    """
    workspace = tmp_path

    # --- Phase 1: typed entries with entity tags -----------------------
    # Pre-cursor entry — consolidated long ago. valid_from in April.
    pre = store_memory(
        workspace,
        content="Marcelo prefiere pytest sobre unittest (early observation).",
        headline="Marcelo testing preference (old)",
        entities=["person:marcelo"],
        valid_from=date(2026, 4, 1),
    )
    # Post-cursor entry — fresh, not yet consolidated. valid_from in late May.
    post = store_memory(
        workspace,
        content="Marcelo confirma uso de pytest en sesión reciente con --xdist flag.",
        headline="Marcelo pytest xdist usage",
        entities=["person:marcelo"],
        valid_from=date(2026, 5, 23),
    )
    # Unrelated — no entity tag.
    other = store_memory(
        workspace,
        content="The webui has a tui fallback when no browser available.",
        headline="webui tui fallback",
        entities=[],
        valid_from=date(2026, 5, 1),
    )

    # --- Phase 1.4 / Phase 2: consolidated entity page ----------------
    page = EntityPage(
        type="person",
        name="Marcelo Marmol",
        aliases=["Marcelo", "marcelo"],
        body="## Current State\nPrefers pytest over unittest.\n",
        extra={"identifiers": ["mmarmol@mxhero.com"]},
    )
    page_path = workspace / "memory" / "entities" / "person" / "marcelo.md"
    page.save(page_path)

    # --- Phase 1.4: build alias_index from the page -------------------
    # (rebuild-only — no persistent sidecar per doc 23 T1.4)
    alias_idx = AliasIndex(workspace / "memory")
    alias_idx.build()

    # --- Phase 3.0: vector index entries + the entity page ------------
    vector_idx = VectorIndex(workspace, provider)
    vector_idx.upsert(load_entry(Path(pre["path"])), pre["class"], Path(pre["path"]))
    vector_idx.upsert(load_entry(Path(post["path"])), post["class"], Path(post["path"]))
    vector_idx.upsert(load_entry(Path(other["path"])), other["class"], Path(other["path"]))
    vector_idx.upsert_entity_page(
        entity_ref="person:marcelo",
        name=page.name,
        aliases=page.aliases,
        body=page.body,
        path=page_path,
    )

    # --- Phase 3.1: extract entities from query -----------------------
    query = "what does Marcelo prefer about testing?"
    query_entities = extract_query_entities(query, alias_idx)
    assert "person:marcelo" in query_entities, "alias_index must catch Marcelo"

    # --- Vector search returns raw candidates -------------------------
    raw_results = vector_idx.search(query, top_k=10)
    assert raw_results, "vector search returned no results"

    # --- Phase 3.2/3.3: apply multi-factor ranking --------------------
    # Memory entries need entities + valid_from carried through for the
    # ranker. Vector index only stores summary/headline/path — we need
    # to enrich with entities from the entry frontmatter.
    enriched: list[dict] = []
    for record in raw_results:
        rec = dict(record)
        if record.get("class_name") in ("episodic", "stable", "corpus", "pending"):
            try:
                entry_path = workspace / record["path"]
                entry = load_entry(entry_path)
                rec["entities"] = entry.entities
                rec["valid_from"] = (
                    entry.valid_from.isoformat() if entry.valid_from else ""
                )
            except Exception:
                rec["entities"] = []
        enriched.append(rec)

    # Two-track model (N3): both pre + post are tagged; recency orders them
    # (post 2026-05-23 ranks above pre 2026-04-01 within the tagged group).
    ranked = rank_with_entities(
        enriched,
        query_entities=query_entities,
        score_field="_distance",
        higher_is_better=False,
    )

    ids = [r.record["id"] for r in ranked]
    print(f"\n=== Phase 3 e2e ranked results ===\n")
    for r in ranked:
        print(f"  {r.adjusted_score:.4f}  {r.record['id']:<60}  {r.signals}")

    # ASSERT 1: the entity page is in the top results (since query matches it).
    assert "person:marcelo" in ids[:3], (
        f"entity page should surface in top 3; got order {ids}"
    )

    # ASSERT 2: the older entry is demoted relative to the newer one by
    # recency (both tagged; the tagged group sorts newest-first).
    pre_pos = ids.index(pre["id"])
    post_pos = ids.index(post["id"])
    assert post_pos < pre_pos, (
        f"newer entry ({post_pos}) should rank above older ({pre_pos}); "
        f"order: {ids}"
    )

    # ASSERT 3: ranking annotated which signals fired. Under RRF (doc 23 T1.3)
    # the page gets vector_rank + entity_page_rank; tagged entries get
    # vector_rank + tagged_rank (two-track model: no cursor exclusion).
    page_result = next(r for r in ranked if r.record["id"] == "person:marcelo")
    assert any("entity_page_rank" in s for s in page_result.signals)

    pre_result = next(r for r in ranked if r.record["id"] == pre["id"])
    assert any("tagged_rank" in s for s in pre_result.signals), (
        "tagged entries get the entity-match boost (recency-ordered)"
    )


def test_alias_via_identifier_finds_page(
    tmp_path: Path, provider: _DeterministicProvider
) -> None:
    """Per Phase 0.1: emails don't have semantic overlap with names in the
    embedding space (cosine 0.27 for ``Marcelo Marmol`` vs ``mmarmol@``).
    The alias_index must bridge this gap structurally."""
    workspace = tmp_path

    page = EntityPage(
        type="person",
        name="Marcelo Marmol",
        aliases=["Marcelo", "marcelo"],
        body="## Background\nFounder.\n",
        extra={"identifiers": ["mmarmol@mxhero.com", "UM7TCSZRN"]},
    )
    page.save(workspace / "memory" / "entities" / "person" / "marcelo.md")

    alias_idx = AliasIndex(workspace / "memory")
    alias_idx.build()

    # Query mentions ONLY the email — embeddings would miss the name link,
    # but alias_index has the identifier and resolves it.
    query = "tell me about mmarmol@mxhero.com history"
    found = extract_query_entities(query, alias_idx)
    assert "person:marcelo" in found
