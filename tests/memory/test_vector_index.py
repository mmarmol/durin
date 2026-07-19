"""Tests for the LanceDB-backed vector index.

These use real lancedb in ``tmp_path`` plus a stubbed fastembed (so we
don't pull 2 GB of model data on every CI run). The embedding provider
returns deterministic vectors keyed off the input text so search
results are predictable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.embedding import EmbeddingProvider
from durin.memory.store import store_memory
from durin.memory.vector_index import VectorIndex, vector_index_available


class _FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic embeddings keyed off the first character of the text.

    Vectors are 8-dim so the records stay small. The first dimension is
    derived from the input — texts starting with the same character
    embed to identical vectors, which lets the tests assert that a
    query for one text retrieves the closest stored text.
    """

    DIM = 8

    @property
    def model_name(self) -> str:
        return "fake/test-embed"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            seed = float(ord(text[0])) if text else 0.0
            out.append([seed] + [0.0] * (self.DIM - 1))
        return out


@pytest.fixture
def provider() -> _FakeEmbeddingProvider:
    return _FakeEmbeddingProvider()


pytestmark = pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb is not installed; install durin[memory] to run these tests",
)


# ---------------------------------------------------------------------------
# upsert / search
# ---------------------------------------------------------------------------


def test_upsert_creates_table_and_returns_via_search(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    result = store_memory(workspace, content="alpha body", headline="alpha")
    entry_path = Path(result["path"])
    from durin.memory.storage import load_entry

    index = VectorIndex(workspace, provider)
    index.upsert(load_entry(entry_path), result["class"], entry_path)

    hits = index.search("alpha query", top_k=5)
    assert hits
    assert hits[0]["headline"] == "alpha"


def test_upsert_entity_page_indexes_without_type_prefix(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """Entity page indexed via name+aliases+body. Phase 0.1 found that
    embedding ``project:durin`` literally hurts recall vs ``durin``."""
    workspace = tmp_path
    page_path = workspace / "memory" / "entities" / "person" / "marcelo.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text("not embedded directly", encoding="utf-8")

    index = VectorIndex(workspace, provider)
    index.upsert_entity_page(
        entity_ref="person:marcelo",
        name="Marcelo Marmol",
        aliases=["Marcelo", "marcelo"],
        body="## Current State\nWorks on durin and mxhero.\n",
        path=page_path,
    )

    hits = index.search("Marcelo", top_k=5)
    assert hits, "page should be findable by name"
    # Entity pages are tagged with class_name="entity_page" so consumers
    # (Phase 3 ranker) can distinguish.
    assert hits[0]["class_name"] == "entity_page"
    assert hits[0]["id"] == "person:marcelo"


def test_upsert_entity_page_idempotent(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    page_path = workspace / "memory" / "entities" / "person" / "marcelo.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text("body", encoding="utf-8")

    index = VectorIndex(workspace, provider)
    for _ in range(3):
        index.upsert_entity_page(
            entity_ref="person:marcelo",
            name="Marcelo",
            aliases=[],
            body="body",
            path=page_path,
        )
    hits = index.search("Marcelo", top_k=20)
    matches = [h for h in hits if h["id"] == "person:marcelo"]
    assert len(matches) == 1, "re-upsert should replace, not duplicate"


def test_upsert_is_idempotent_on_id(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """Re-upserting the same id replaces the prior row, doesn't duplicate."""
    workspace = tmp_path
    result = store_memory(workspace, content="content one", headline="first")
    entry_path = Path(result["path"])
    from durin.memory.storage import load_entry

    index = VectorIndex(workspace, provider)
    entry = load_entry(entry_path)
    index.upsert(entry, result["class"], entry_path)
    index.upsert(entry, result["class"], entry_path)

    hits = index.search("content", top_k=10)
    assert sum(1 for h in hits if h["id"] == entry.id) == 1


def test_search_returns_top_k_by_similarity(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    items = [
        ("alpha body content", "alpha-headline"),
        ("beta body content", "beta-headline"),
        ("gamma body content", "gamma-headline"),
    ]
    index = VectorIndex(workspace, provider)
    from durin.memory.storage import load_entry

    for content, headline in items:
        r = store_memory(workspace, content=content, headline=headline)
        index.upsert(load_entry(Path(r["path"])), r["class"], Path(r["path"]))

    # Query starting with 'g' should pull the 'gamma' record first
    # because the fake provider keys off the first character.
    hits = index.search("gamma question", top_k=1)
    assert len(hits) == 1
    assert hits[0]["headline"] == "gamma-headline"


def test_search_empty_query_returns_empty(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    index = VectorIndex(tmp_path, provider)
    assert index.search("") == []
    assert index.search("   ") == []
    assert index.search("anything", top_k=0) == []


def test_search_missing_table_returns_empty(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """No upserts yet → search returns []."""
    index = VectorIndex(tmp_path, provider)
    assert index.search("query") == []


def test_search_strips_vector_from_results(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    r = store_memory(workspace, content="x", headline="h")
    from durin.memory.storage import load_entry

    index = VectorIndex(workspace, provider)
    index.upsert(load_entry(Path(r["path"])), r["class"], Path(r["path"]))

    hits = index.search("query")
    for hit in hits:
        assert "vector" not in hit, "raw vector column must not leak to callers"


# ---------------------------------------------------------------------------
# rebuild_from_workspace
# ---------------------------------------------------------------------------


def test_rebuild_walks_all_classes(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    store_memory(workspace, content="a", headline="A", class_name="stable")
    store_memory(workspace, content="b", headline="B", class_name="episodic")
    store_memory(workspace, content="c", headline="C", class_name="corpus")

    index = VectorIndex(workspace, provider)
    count = index.rebuild_from_workspace()
    assert count == 3

    hits = index.search("query", top_k=10)
    assert len(hits) == 3
    headlines = {h["headline"] for h in hits}
    assert headlines == {"A", "B", "C"}


def test_rebuild_drops_stale_entries(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """Rebuild rebuilds: stale entries from a prior rebuild are gone."""
    workspace = tmp_path
    r1 = store_memory(workspace, content="first", headline="FIRST")
    from durin.memory.storage import load_entry

    index = VectorIndex(workspace, provider)
    index.upsert(load_entry(Path(r1["path"])), r1["class"], Path(r1["path"]))

    # Now remove the on-disk markdown file but keep the prior index row.
    Path(r1["path"]).unlink()
    assert not Path(r1["path"]).is_file()

    count = index.rebuild_from_workspace()
    assert count == 0
    assert index.search("first") == []


def test_rebuild_empty_workspace_drops_table(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    index = VectorIndex(tmp_path, provider)
    count = index.rebuild_from_workspace()
    assert count == 0
    assert index.search("anything") == []


def test_rebuild_skips_malformed_files(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """A broken file must not crash the rebuild — it's just skipped."""
    workspace = tmp_path
    store_memory(workspace, content="good", headline="GOOD")
    # Drop a malformed file alongside the good one.
    bad_dir = workspace / "memory" / "stable"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")

    index = VectorIndex(workspace, provider)
    count = index.rebuild_from_workspace()
    assert count == 1


# ---------------------------------------------------------------------------
# embed_text composition rule
# ---------------------------------------------------------------------------


def test_embed_text_composes_all_fields_in_order() -> None:
    from durin.memory.schema import MemoryEntry

    entry = MemoryEntry(
        id="x",
        headline="hed",
        summary="sum",
        entities=["person:marcelo", "project:durin"],
        body="bod",
    )
    text = VectorIndex._embed_text(entry)
    # Order matters: headline first so the embedder sees the most
    # distilled signal up front; body last as the longest, most
    # truncatable component.
    assert text.index("hed") < text.index("sum")
    assert text.index("sum") < text.index("Entities:")
    assert text.index("Entities:") < text.index("bod")
    assert "person:marcelo" in text
    assert "project:durin" in text


def test_embed_text_skips_empty_fields() -> None:
    from durin.memory.schema import MemoryEntry

    only_body = MemoryEntry(id="x", headline=" ", body="bod")
    assert VectorIndex._embed_text(only_body) == "bod"

    no_entities = MemoryEntry(id="x", headline="hed", summary="sum", body="bod")
    text = VectorIndex._embed_text(no_entities)
    assert "Entities:" not in text
    assert text == "hed\n\nsum\n\nbod"


def test_embed_text_fallbacks_when_all_empty() -> None:
    from durin.memory.schema import MemoryEntry

    empty = MemoryEntry(id="x", headline="")
    assert VectorIndex._embed_text(empty) == "memory entry"


def test_embed_text_respects_char_budget() -> None:
    from durin.memory.schema import MemoryEntry

    entry = MemoryEntry(
        id="x",
        headline="H" * 100,
        summary="S" * 500,
        body="B" * 5000,
    )
    text = VectorIndex._embed_text(entry, budget_chars=200)
    # Budget is a hard cap on the composed text length (joiners count).
    assert len(text) <= 200
    # Headline survives (was 100 chars, fits comfortably) and a portion
    # of the next field appears before the budget is hit.
    assert text.startswith("H" * 100)


# ---------------------------------------------------------------------------
# Audit H4 (2026-05-29): summary fallback at vector-index write time
# ---------------------------------------------------------------------------
#
# Context: bulk-imported entries (bench seeds, memory_ingest chunks,
# raw episodic turns) leave ``summary=''`` because no LLM call runs on
# the write path — Dream was the intended summary author but doesn't
# process corpus and only consolidates episodic into entity_pages
# (the source episodic stays with ``summary=''`` forever). Pre-H4 the
# warm-tier renderer fell back to ``snippet or headline`` and the agent
# saw a 60-char headline as the only triage signal, drilling repeatedly.
#
# H4 makes the vector index materialise ``body[:400]`` into the LanceDB
# row's ``summary`` field when the source entry has none. The file on
# disk stays the source of truth (``summary: ''`` remains a legitimate
# state); the index carries a derived value so search results never
# hand the LLM an empty summary. When Dream or memory_store later
# populates the source's real summary, the row gets re-upserted with
# the authoritative value.


def _store_entry_with_body(workspace: Path, *, body: str, headline: str,
                            summary: str = "") -> Path:
    """Helper: persist a memory entry with the requested body/summary
    on disk, return its path. Mirrors what store_memory does but lets
    us set ``summary`` explicitly (the public API also accepts it)."""
    from durin.memory.store import store_memory
    result = store_memory(
        workspace, content=body, headline=headline, summary=summary,
    )
    return Path(result["path"])


def test_vector_row_derives_summary_from_body_when_source_empty(
    tmp_path: Path, provider: _FakeEmbeddingProvider,
) -> None:
    """Source entry has summary='' (the bench / ingest reality). The
    LanceDB row carries body[:400] as the materialised fallback so the
    renderer can hand the LLM real triage content."""
    long_body = (
        "Joanna keeps her stuffed animal dog Tilly with her while she "
        "writes. Tilly helps her stay focused and brings her so much joy. "
        "Nate gifted Tilly to Joanna because she had to give up her real "
        "dog when she moved to Michigan. " * 3
    )
    entry_path = _store_entry_with_body(
        tmp_path, body=long_body, headline="Joanna: Tilly helps me stay focu",
        summary="",
    )
    from durin.memory.storage import load_entry
    entry = load_entry(entry_path)
    assert entry.summary == "", "fixture precondition: source summary empty"

    index = VectorIndex(tmp_path, provider)
    index.upsert(entry, "episodic", entry_path)

    hits = index.search("Joanna writing", top_k=5)
    assert hits
    row_summary = hits[0]["summary"]
    assert row_summary, "vector row must materialise a non-empty summary"
    assert row_summary == long_body[:400], (
        f"expected body[:400] fallback; got {row_summary[:60]!r}…"
    )


def test_vector_row_preserves_authoritative_summary(
    tmp_path: Path, provider: _FakeEmbeddingProvider,
) -> None:
    """When the source has a real summary (Dream or memory_store
    explicit), the index must NOT overwrite it with the body prefix."""
    real_summary = (
        "Joanna writes movie scripts and keeps Tilly the stuffed animal "
        "with her for focus and emotional support."
    )
    entry_path = _store_entry_with_body(
        tmp_path,
        body="Some longer body text that should not become the summary.",
        headline="Joanna writing summary",
        summary=real_summary,
    )
    from durin.memory.storage import load_entry
    entry = load_entry(entry_path)
    index = VectorIndex(tmp_path, provider)
    index.upsert(entry, "episodic", entry_path)

    hits = index.search("Joanna", top_k=5)
    assert hits
    assert hits[0]["summary"] == real_summary, (
        "authoritative summary must survive the write path"
    )


def test_vector_row_handles_body_shorter_than_fallback(
    tmp_path: Path, provider: _FakeEmbeddingProvider,
) -> None:
    """Bench seed entries are typically <300 chars. The fallback must
    clip safely — summary == body when body fits, no out-of-bounds."""
    short_body = "Joanna: Tilly is a stuffed dog. She helps me stay focused."
    entry_path = _store_entry_with_body(
        tmp_path, body=short_body, headline="Joanna short",
    )
    from durin.memory.storage import load_entry
    entry = load_entry(entry_path)
    index = VectorIndex(tmp_path, provider)
    index.upsert(entry, "episodic", entry_path)

    hits = index.search("Joanna", top_k=5)
    assert hits[0]["summary"] == short_body, (
        f"short body should appear verbatim; got {hits[0]['summary']!r}"
    )


def test_embed_text_skips_fallback_summary_to_avoid_duplication() -> None:
    """When ``summary == body[:N]`` (the H4 fallback marker), composing
    ``headline + summary + entities + body`` would embed the first N
    chars twice — once in the summary slot and again at the start of
    body. The embedding text must detect this and skip the summary
    slot so the budget goes to unique content instead of repeating
    the body prefix."""
    from durin.memory.schema import MemoryEntry

    body = "X" * 1000
    fallback_summary = body[:400]
    entry = MemoryEntry(
        id="x",
        headline="head",
        summary=fallback_summary,
        body=body,
    )
    text = VectorIndex._embed_text(entry)
    # Headline first, then body (skipping summary slot).
    assert text.startswith("head")
    # The fallback summary block ("X"*400) must NOT appear as a separate
    # joined paragraph — body starts immediately after the headline
    # joiner, not after a duplicated 400-X block.
    # Quick invariant: total length is roughly head + joiner + body_clipped,
    # NOT head + joiner + 400X + joiner + body_clipped.
    assert "X" * 401 in text, "body content must reach the embedder"
    # The summary slot's exact opening ("\n\n" + 400X + "\n\n" + same-X-prefix)
    # would mean the prefix appears separately. Detect with a quick check:
    # if summary slot was added, we'd have len(text) ~> 60+1500 budget hit
    # with prefix duplicated. Detect more robustly: search for two distinct
    # 400-X blocks separated by a joiner.
    duplicated_marker = ("X" * 400) + "\n\n" + ("X" * 400)
    assert duplicated_marker not in text, (
        "fallback summary must not be re-embedded as its own slot"
    )


def test_embed_text_includes_summary_when_authoritative() -> None:
    """When summary carries new semantic info (Dream output, not a
    body prefix), it MUST be embedded — that's the whole point of
    persisting it. The dedup heuristic must trigger ONLY on the
    body-prefix case."""
    from durin.memory.schema import MemoryEntry

    entry = MemoryEntry(
        id="x",
        headline="head",
        summary="distinct semantic summary not present in body",
        body="entirely different body text about other matters",
    )
    text = VectorIndex._embed_text(entry)
    assert "distinct semantic summary" in text
    assert "entirely different body text" in text


def test_upsert_is_atomic_keeps_existing_row_when_insert_fails(
    tmp_path: Path, provider: _FakeEmbeddingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B6: upsert must not lose a pre-existing row if the write fails midway.

    The old delete-then-add did two commits, so a failure after the delete
    left the row gone. An atomic ``merge_insert`` never calls ``table.add``
    and applies as a single commit, so a forced ``add`` failure can't strand
    the index. Patching ``add`` to raise reproduces the old data-loss window
    (RED) and is a no-op once the write is atomic (GREEN).
    """
    from durin.memory.storage import load_entry
    from durin.memory.vector_index import _TABLE_NAME

    workspace = tmp_path
    result = store_memory(workspace, content="alpha body", headline="alpha")
    entry_path = Path(result["path"])
    entry = load_entry(entry_path)

    index = VectorIndex(workspace, provider)
    index.upsert(entry, result["class"], entry_path)
    assert index.search("alpha query", top_k=5), "row should be present after first upsert"

    table_cls = type(index._connect().open_table(_TABLE_NAME))

    def _boom(self, *args, **kwargs):  # noqa: ANN001
        raise RuntimeError("simulated insert failure")

    monkeypatch.setattr(table_cls, "add", _boom)

    try:
        index.upsert(entry, result["class"], entry_path)
    except RuntimeError:
        pass  # old path raises after the destructive delete; new path won't

    assert index.search("alpha query", top_k=5), "existing row must survive a failed re-upsert"


@pytest.mark.skipif(
    not vector_index_available(), reason="lancedb not installed",
)
def test_compact_index_prunes_versions(tmp_path):
    """Churned tables accrete one version per write forever (2930 on the
    2026-07-18 box); the nightly compaction prunes them to a handful."""
    from durin.memory.vector_index import compact_index

    ws = tmp_path / "ws"
    vi = VectorIndex(ws, _FakeEmbeddingProvider())
    for i in range(8):
        vi.upsert_entity_page(
            entity_ref=f"topic:t{i}", name=f"T{i}", aliases=[],
            body=f"body {i}",
            path=ws / "memory" / "entities" / "topic" / f"t{i}.md",
        )
    stats = compact_index(ws)
    assert stats["compacted"] is True
    assert stats["versions_before"] > stats["versions_after"]
    # count_rows survives even on a corrupted rewrite — the promise is that
    # VECTOR SEARCH still works after maintenance.
    hits = vi.search("body 3", top_k=3)
    assert hits

    missing = compact_index(tmp_path / "empty-ws")
    assert missing == {"compacted": False, "reason": "no_index"}
