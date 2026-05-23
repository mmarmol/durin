"""Phase 6 — acceptance tests for the entity-centric memory outcomes.

Each test asserts one of the outcomes from
``docs/18_entity_centric_plan.md`` §11 — the operational promises the
architecture must deliver on. These are integration tests that wire
together phases 1-5 with stub LLMs (so they're deterministic and fast)
and fake embeddings (no real fastembed download).

If any of these tests fail, the architecture has regressed on a
load-bearing promise; the right move is to reopen doc 18 (or doc 19's
asunción master list) with the new evidence.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from durin.memory.absorption import EntityAbsorption
from durin.memory.aliases_index import AliasIndex
from durin.memory.dream import ConsolidationResult, DreamConsolidator, EntryRef
from durin.memory.embedding import EmbeddingProvider
from durin.memory.entity_page import EntityPage
from durin.memory.entity_ranker import (
    extract_query_entities,
    rank_with_entities,
)
from durin.memory.store import store_memory
from durin.memory.storage import load_entry
from durin.memory.vector_index import VectorIndex, vector_index_available
from durin.utils.git_repo import GitRepo


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _CharProvider(EmbeddingProvider):
    """Embeds by counting alpha chars into a 16-dim bucket. Deterministic."""

    DIM = 16

    @property
    def model_name(self) -> str:
        return "fake/char-bucket"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.DIM
            for ch in text.lower():
                if ch.isalpha():
                    vec[(ord(ch) - ord("a")) % self.DIM] += 1.0
            out.append(vec)
        return out


def _make_stub_llm(entity_ref: str, *, body: str, sources: list[str], cursor: int):
    """Builds an LLM stub that returns a well-formed consolidation response."""
    type_, slug = entity_ref.split(":", 1)
    source_str = ", ".join(sources)
    response = (
        "===PAGE===\n"
        "---\n"
        f"type: {type_}\n"
        f"name: {slug.replace('_', ' ').title()}\n"
        f"aliases: [{slug}, {slug.title()}]\n"
        f"dream_processed_through: {cursor}\n"
        "---\n"
        "\n"
        f"# {slug.replace('_', ' ').title()}\n"
        "\n"
        f"{body}\n"
        "===COMMIT===\n"
        f"Consolidate {entity_ref} (rev 1)\n"
        "\n"
        "Consolidation pass merging episodic observations.\n"
        "\n"
        f"Sources: {source_str}\n"
        f"Entities-touched: {entity_ref}\n"
        f"Cursor-after: {cursor}\n"
        "===END===\n"
    )
    return lambda prompt, *, model: response


# ---------------------------------------------------------------------------
# O1 — Coherencia cross-sesión sobre proyecto
# ---------------------------------------------------------------------------


def test_o1_project_decisions_consolidated(tmp_path: Path) -> None:
    """Outcome: after multiple sessions touching project:durin, a query
    about decisions surfaces the consolidated entity page (not just
    raw episodic entries via grep)."""
    # Seed 5 entries across "sessions" each making a decision about
    # project:durin.
    for i, content in enumerate([
        "Decidimos usar pytest sobre unittest en project:durin.",
        "Decisión: usar paraphrase-multilingual como embedding default.",
        "Decidimos diferir bi-temporal validity hasta tener uso real.",
        "Decisión: alias_index devuelve lista para manejar colisiones.",
        "Decidimos usar dulwich para git en lugar de subprocess git.",
    ]):
        store_memory(
            tmp_path,
            content=content,
            headline=f"durin decision {i}",
            entities=["project:durin"],
            valid_from=date(2026, 4, 1 + i),
        )

    # Dream consolidates project:durin.
    body = (
        "## Decisions\n\n"
        "- pytest over unittest\n"
        "- paraphrase-multilingual embeddings\n"
        "- bi-temporal validity deferred\n"
        "- alias_index returns list\n"
        "- dulwich for git\n"
    )
    consolidator = DreamConsolidator(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(
            "project:durin", body=body,
            sources=["d1", "d2", "d3", "d4", "d5"], cursor=50,
        ),
    )
    result = consolidator.consolidate_entity(
        "project:durin",
        [EntryRef(id=f"d{i}", timestamp=f"2026-04-0{i}",
                  text="placeholder") for i in range(1, 6)],
    )
    consolidator.apply("project:durin", result)

    # Page exists on disk
    page_path = tmp_path / "memory" / "entities" / "project" / "durin.md"
    assert page_path.exists()
    page = EntityPage.from_file(page_path)
    assert page is not None
    # The decisions content survived consolidation
    assert "pytest" in page.body
    assert "embeddings" in page.body
    assert "dulwich" in page.body

    # Alias index now resolves "durin" to the canonical entity
    # (rebuild from disk per doc 23 T1.4 — no persistent sidecar)
    idx = AliasIndex(tmp_path / "memory")
    idx.build()
    query_entities = extract_query_entities("¿qué decisiones tomamos sobre durin?", idx)
    assert "project:durin" in query_entities


# ---------------------------------------------------------------------------
# O2 — Unificación automática por aliases / identifiers
# ---------------------------------------------------------------------------


def test_o2_aliases_and_identifiers_unify(tmp_path: Path) -> None:
    """Outcome: name forms and email forms BOTH resolve to the same
    entity in the alias_index, without manual intervention.

    Phase 0.1 showed embeddings can't bridge name↔email (cosine 0.27).
    The alias_index/identifiers field must bridge structurally."""
    # Build a consolidated page with both representations as identifiers.
    page = EntityPage(
        type="person",
        name="Marcelo Marmol",
        aliases=["Marcelo", "marcelo"],
        extra={"identifiers": ["mmarmol@mxhero.com", "UM7TCSZRN"]},
        body="## Background\nFounder of mxhero.\n",
    )
    page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")

    idx = AliasIndex(tmp_path / "memory")
    idx.build()

    # Both query forms resolve to the same entity
    assert extract_query_entities("ask Marcelo", idx) == ["person:marcelo"]
    assert extract_query_entities(
        "user mmarmol@mxhero.com is in slack", idx
    ) == ["person:marcelo"]
    assert extract_query_entities(
        "Slack user UM7TCSZRN said hi", idx
    ) == ["person:marcelo"]


# ---------------------------------------------------------------------------
# O3 — Incidente recurrente consolidado
# ---------------------------------------------------------------------------


def test_o3_recurring_incident_consolidated(tmp_path: Path) -> None:
    """Outcome: when 3 sessions mention the same bug pattern, the dream
    can consolidate it into a single event page with cause + fix
    structure."""
    for i, content in enumerate([
        "TUI showed empty bubbles when streaming markdown.",
        "TUI bubbles again empty during long answers — reproduces.",
        "TUI empty bubbles fix: escape body markup + tighten consumer.",
    ]):
        store_memory(
            tmp_path,
            content=content,
            headline=f"TUI bubble issue obs {i}",
            entities=["event:tui-bug-empty-bubbles"],
            valid_from=date(2026, 5, 10 + i),
        )

    body = (
        "## Symptom\nEmpty bubbles in TUI during markdown streaming.\n\n"
        "## Cause\nMarkup escaping was incomplete; consumer raced ahead.\n\n"
        "## Fix\nEscape body markup + harden outbound consumer.\n"
    )
    consolidator = DreamConsolidator(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(
            "event:tui-bug-empty-bubbles", body=body,
            sources=["t1", "t2", "t3"], cursor=30,
        ),
    )
    result = consolidator.consolidate_entity(
        "event:tui-bug-empty-bubbles",
        [
            EntryRef(id="t1", timestamp="2026-05-10", text="obs 1"),
            EntryRef(id="t2", timestamp="2026-05-11", text="obs 2"),
            EntryRef(id="t3", timestamp="2026-05-12", text="obs 3"),
        ],
    )
    consolidator.apply("event:tui-bug-empty-bubbles", result)

    page = EntityPage.from_file(
        tmp_path / "memory" / "entities" / "event" / "tui-bug-empty-bubbles.md"
    )
    assert page is not None
    # The consolidation captured cause + fix structure
    assert "Symptom" in page.body
    assert "Cause" in page.body
    assert "Fix" in page.body


# ---------------------------------------------------------------------------
# O4 — Drill-down "why": git log shows reasoning per revision
# ---------------------------------------------------------------------------


def test_o4_drill_down_why_via_git_log(tmp_path: Path) -> None:
    """Outcome: `durin memory history <entity>` returns commits with the
    LLM's reasoning preserved + structured trailers parseable."""
    # Do TWO consolidations so there are 2 revisions to inspect.
    consolidator = DreamConsolidator(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(
            "person:marcelo", body="Rev1 content.",
            sources=["e1"], cursor=10,
        ),
    )
    r1 = consolidator.consolidate_entity(
        "person:marcelo",
        [EntryRef(id="e1", timestamp="2026-04-10", text="obs1")],
    )
    consolidator.apply("person:marcelo", r1)

    consolidator._llm_invoke = _make_stub_llm(
        "person:marcelo", body="Rev2 content.",
        sources=["e2"], cursor=20,
    )
    r2 = consolidator.consolidate_entity(
        "person:marcelo",
        [EntryRef(id="e2", timestamp="2026-04-15", text="obs2")],
    )
    consolidator.apply("person:marcelo", r2)

    repo = GitRepo(tmp_path / "memory")
    page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    commits = repo.log(page_path)
    # 2 consolidations + (possibly the init commit which doesn't touch the page)
    consolidation_commits = [c for c in commits if "Consolidate" in c.subject]
    assert len(consolidation_commits) == 2
    # Both have body explaining the reason
    for c in consolidation_commits:
        assert c.body, f"commit {c.sha[:8]} missing body"
        assert "Sources" in c.trailers
        assert "Cursor-after" in c.trailers


# ---------------------------------------------------------------------------
# O5 — Drill-down "expand": sources, related, archive
# ---------------------------------------------------------------------------


def test_o5_drill_down_expand(tmp_path: Path) -> None:
    """Outcome: after absorption, the canonical page's expansion reveals
    the archive subfolder + the absorbed page's history."""
    # Build canonical + absorbed via direct page saves
    EntityPage(
        type="person", name="Marcelo Marmol", aliases=["Marcelo"],
        body="## Background\n", extra={"identifiers": ["mmarmol@mxhero.com"]},
    ).save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    EntityPage(
        type="person", name="Marcelo M.", aliases=["Marcelo"],
        body="## Notes\nAbsorbed observation.",
        extra={"identifiers": ["UM7TCSZRN"]},
    ).save(tmp_path / "memory" / "entities" / "person" / "marcelo_m.md")

    absorber = EntityAbsorption(tmp_path)
    absorber.absorb("person:marcelo", "person:marcelo_m",
                    reason="duplicate identity confirmed")

    archive_path = (
        tmp_path / "memory" / "entities" / "person" / "marcelo" / "archive"
        / "marcelo_m.md"
    )
    assert archive_path.exists(), "absorbed page must be in archive subfolder"
    archived = EntityPage.from_file(archive_path)
    assert archived is not None
    # Drill-down via the absorbed_into pointer works:
    assert archived.extra["absorbed_into"] == "../../marcelo.md"

    # The canonical's body now references the absorbed entity:
    canonical = EntityPage.from_file(
        tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    )
    assert canonical is not None
    assert "Absorbed from person:marcelo_m" in canonical.body


# ---------------------------------------------------------------------------
# Anti-fragilidad — el sistema funciona aún sin dream activo
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb is not installed",
)
def test_anti_fragility_no_dream_still_searchable(tmp_path: Path) -> None:
    """Phase 6.3 anti-fragility: if dream never runs, entries with
    entity tags are STILL retrievable. No consolidation needed for
    base function to work — read-time reconciliation (doc 18 §3.4)
    means the system degrades softly, not breaks."""
    # Store entries about person:marcelo — but NO dream consolidation
    for i, content in enumerate([
        "Marcelo prefiere pytest sobre unittest.",
        "Marcelo usa --xdist para test paralelos.",
        "Marcelo es founder de mxhero (mmarmol@mxhero.com).",
    ]):
        store_memory(
            tmp_path,
            content=content,
            headline=f"Marcelo obs {i}",
            entities=["person:marcelo"],
            valid_from=date(2026, 5, 20 + i),
        )

    # No entity page exists. alias_index built from empty entities/ is empty.
    idx = AliasIndex(tmp_path / "memory")
    idx.build()
    # The alias_index has nothing because no entity page → empty lookup.
    assert idx.lookup("Marcelo") == []

    # BUT the entries themselves are searchable via vector_index.
    provider = _CharProvider()
    index = VectorIndex(tmp_path, provider)
    from durin.memory.store import store_memory as _  # ensure import path
    # Index the entries we stored
    for class_dir in (tmp_path / "memory" / "episodic").glob("*.md"):
        entry = load_entry(class_dir)
        index.upsert(entry, "episodic", class_dir)

    # Query surfaces the raw entries — system function preserved
    hits = index.search("Marcelo pytest preference", top_k=5)
    assert hits, "raw entries must be findable even without consolidation"
    # All 3 entries should be in the index
    assert len(hits) == 3
