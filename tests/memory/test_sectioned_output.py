"""Sectioned output renderer.

Groups final top-K hits by source class, applies the per-source cap
for corpus chunks, and renders structural marker blocks the agent
can parse:

    === CANONICAL: <uri> (consolidated <ts>) ===
    === FRAGMENT: <path> (ts <ts>) ===
    === SESSION: <session_id>/<turn_or_summary> (ts <ts>) ===
    === INGESTED: <ingest_id>/<chunk_or_source> ===

Sections with zero hits are omitted entirely.
"""

from __future__ import annotations

from durin.memory.sectioned_output import (
    DEFAULT_MAX_PER_SOURCE,
    SectionedHit,
    apply_per_source_cap,
    render_sectioned,
)


def _h(
    uri: str, type_: str, *,
    path: str = "",
    score: float = 1.0,
    ts: str = "2026-05-26T10:00:00",
    snippet: str = "",
    summary: str = "",
    ingest_id: str | None = None,
) -> SectionedHit:
    return SectionedHit(
        uri=uri, type=type_, path=path or f"memory/{type_}/{uri}.md",
        score=score, ts=ts, snippet=snippet, summary=summary,
        ingest_id=ingest_id,
    )


# ---------------------------------------------------------------------------
# Per-source cap
# ---------------------------------------------------------------------------


class TestPerSourceCap:
    def test_no_corpus_hits_no_cap(self) -> None:
        hits = [
            _h("person:m", "entity"),
            _h("e1", "episodic"),
        ]
        assert apply_per_source_cap(hits) == hits

    def test_corpus_under_cap_unchanged(self) -> None:
        hits = [
            _h("c1", "corpus", ingest_id="paper_a"),
            _h("c2", "corpus", ingest_id="paper_a"),
        ]
        assert apply_per_source_cap(hits) == hits

    def test_corpus_above_cap_keeps_top_n(self) -> None:
        hits = [
            _h(f"c{i}", "corpus", score=1.0 - i * 0.1,
               ingest_id="paper_a")
            for i in range(6)
        ]
        capped = apply_per_source_cap(hits, max_per_source=3)
        assert len(capped) == 3
        assert [h.uri for h in capped] == ["c0", "c1", "c2"]

    def test_distinct_ingest_ids_independent(self) -> None:
        hits = (
            [_h(f"a{i}", "corpus", score=1.0 - i * 0.01,
                ingest_id="paper_a") for i in range(5)]
            + [_h(f"b{i}", "corpus", score=0.5 - i * 0.01,
                  ingest_id="paper_b") for i in range(5)]
        )
        capped = apply_per_source_cap(hits, max_per_source=3)
        a_count = sum(1 for h in capped if h.uri.startswith("a"))
        b_count = sum(1 for h in capped if h.uri.startswith("b"))
        assert a_count == 3
        assert b_count == 3

    def test_entity_class_not_capped(self) -> None:
        """Only corpus gets capped; entity hits are uncapped."""
        hits = [_h(f"person:p{i}", "entity") for i in range(10)]
        assert apply_per_source_cap(hits, max_per_source=3) == hits

    def test_episodic_not_capped(self) -> None:
        hits = [_h(f"e{i}", "episodic") for i in range(10)]
        assert apply_per_source_cap(hits, max_per_source=3) == hits

    def test_default_cap_is_three(self) -> None:
        assert DEFAULT_MAX_PER_SOURCE == 3


# ---------------------------------------------------------------------------
# Section rendering
# ---------------------------------------------------------------------------


class TestRender:
    def test_canonical_marker_format(self) -> None:
        hits = [_h("person:marcelo", "entity", ts="2026-05-26T10:00:00",
                   snippet="Marcelo (architect)")]
        out = render_sectioned(hits)
        assert (
            "=== CANONICAL: person:marcelo (consolidated 2026-05-26T10:00:00) ==="
            in out
        )
        assert "Marcelo (architect)" in out

    def test_fragment_marker_format(self) -> None:
        hits = [_h(
            "ep1", "episodic",
            path="memory/episodic/ep1.md",
            ts="2026-05-26T11:00:00",
            snippet="recent observation",
        )]
        out = render_sectioned(hits)
        assert (
            "=== FRAGMENT: memory/episodic/ep1.md (ts 2026-05-26T11:00:00) ==="
            in out
        )

    def test_session_marker_format(self) -> None:
        hits = [_h(
            "session:abc/summary", "session_summary",
            ts="2026-05-26T09:00:00",
            snippet="session summary",
        )]
        out = render_sectioned(hits)
        assert "=== SESSION: session:abc/summary (ts 2026-05-26T09:00:00) ===" in out

    def test_ingested_marker_format(self) -> None:
        hits = [_h(
            "corpus:doc/chunk-3", "corpus",
            ingest_id="doc",
            snippet="text chunk",
        )]
        out = render_sectioned(hits)
        assert "=== INGESTED: doc/corpus:doc/chunk-3 ===" in out

    def test_empty_hits_returns_empty_string(self) -> None:
        assert render_sectioned([]) == ""

    def test_section_with_zero_hits_omitted(self) -> None:
        """If only entity hits exist, no fragment/session/ingested
        section headers appear."""
        hits = [_h("person:m", "entity")]
        out = render_sectioned(hits)
        assert "=== CANONICAL:" in out
        assert "=== FRAGMENT:" not in out
        assert "=== SESSION:" not in out
        assert "=== INGESTED:" not in out

    def test_section_order_canonical_fragment_session_ingested(self) -> None:
        """Sections appear in the correct order regardless of input order:
        canonical → fragment → session → ingested."""
        hits = [
            _h("corpus:c", "corpus", ingest_id="ig"),
            _h("session:s/x", "session_summary"),
            _h("e1", "episodic"),
            _h("person:m", "entity"),
        ]
        out = render_sectioned(hits)
        i_can = out.index("=== CANONICAL:")
        i_frag = out.index("=== FRAGMENT:")
        i_sess = out.index("=== SESSION:")
        i_ing = out.index("=== INGESTED:")
        assert i_can < i_frag < i_sess < i_ing

    def test_within_section_sorted_by_score_desc(self) -> None:
        hits = [
            _h("person:low", "entity", score=0.1, snippet="LOW"),
            _h("person:high", "entity", score=0.9, snippet="HIGH"),
            _h("person:mid", "entity", score=0.5, snippet="MID"),
        ]
        out = render_sectioned(hits)
        assert out.index("HIGH") < out.index("MID") < out.index("LOW")

    def test_stable_class_renders_as_fragment(self) -> None:
        """Stable entries render as FRAGMENTs (same section as episodic)."""
        hits = [_h("s1", "stable", path="memory/stable/s1.md")]
        out = render_sectioned(hits)
        assert "=== FRAGMENT: memory/stable/s1.md" in out


# ---------------------------------------------------------------------------
# Prohibition — no valuative language in section headers
# ---------------------------------------------------------------------------


def test_section_intro_has_no_valuative_language() -> None:
    """Section intro sentences carry descriptive metadata only —
    no "trust this" / "authoritative" / similar."""
    hits = [_h("person:m", "entity")]
    out = render_sectioned(hits).lower()
    for forbidden in ("authoritative", "trust this", "treat as"):
        assert forbidden not in out


# ---------------------------------------------------------------------------
# Audit H5 (2026-05-29): per-hit completeness signal in renderer
# ---------------------------------------------------------------------------
#
# The renderer must compute and surface the completeness qualifier so
# the LLM can decide whether drilling is warranted. Three cases:
# - body_length unknown (legacy / lexical-only hits): omit signal.
# - len(summary) >= body_length: rendered content IS the whole body →
#   "complete" — drill returns the same text.
# - len(summary) < body_length: rendered content is a preview →
#   "preview N/M" where N=len(summary), M=body_length.


def test_render_marks_complete_when_summary_covers_body() -> None:
    """Body fits entirely in the H4 summary fallback (typical for
    bench seeds / short turns). The marker carries `complete` so the
    LLM doesn't waste a drill on identical content."""
    full = "Joanna keeps Tilly with her while writing."
    hit = SectionedHit(
        uri="x", type="episodic", path="memory/episodic/x.md",
        score=0.5, ts="2026-05-23",
        summary=full, body_length=len(full),
    )
    out = render_sectioned([hit])
    assert "complete)" in out, (
        "marker must carry the `complete` qualifier when "
        "summary covers the full body"
    )


def test_render_marks_preview_when_body_exceeds_summary() -> None:
    """Body is longer than the summary slice — the LLM gets `preview
    400/1500` so it knows drilling reveals more content."""
    hit = SectionedHit(
        uri="x", type="corpus", path="memory/corpus/x.md",
        score=0.5, ts="2026-05-23",
        summary="lead summary of 28 chars here",
        body_length=1500,
        ingest_id="doc-42",
    )
    out = render_sectioned([hit])
    assert "preview 29/1500)" in out, (
        f"marker must show preview length / total body length;\n"
        f"actual block: {out!r}"
    )


def test_render_omits_completeness_when_body_length_unknown() -> None:
    """Backward compat: hits without ``body_length`` (lexical-only,
    legacy callers) must render with the pre-H5 marker shape."""
    hit = SectionedHit(
        uri="x", type="episodic", path="memory/episodic/x.md",
        score=0.5, ts="2026-05-23",
        summary="some summary",
        body_length=0,
    )
    out = render_sectioned([hit])
    assert "complete" not in out
    assert "preview" not in out


def test_render_marks_complete_for_canonical_entity_pages() -> None:
    """Entity-page hits go through the canonical_marker path; same
    completeness signal applies."""
    full = "Marcelo Marmol: architect of durin."
    hit = SectionedHit(
        uri="person:marcelo", type="entity",
        path="memory/entities/person/marcelo.md",
        score=0.5, ts="2026-05-23T18:00",
        summary=full, body_length=len(full),
    )
    out = render_sectioned([hit])
    assert "complete)" in out


class TestSourcesLine:
    """A canonical entity hit surfaces its ``derived_from`` as a Sources line so
    the agent sees the source documents and can drill them."""

    def test_canonical_hit_renders_sources_line(self) -> None:
        hit = SectionedHit(
            uri="patient:drako", type="entity",
            path="memory/entities/patient/drako.md", score=1.0,
            summary="A canine patient.",
            derived_from=("reference:paper-a", "reference:paper-b"),
        )
        out = render_sectioned([hit])
        assert "Sources: reference:paper-a, reference:paper-b." in out

    def test_no_sources_line_without_derived_from(self) -> None:
        hit = SectionedHit(
            uri="topic:x", type="entity",
            path="memory/entities/topic/x.md", score=1.0, summary="X",
        )
        assert "Sources:" not in render_sectioned([hit])


# ---------------------------------------------------------------------------
# Task 2 (2026-09-08): `max_chars` bounds a rendering's total size — once
# the running length would exceed it, every remaining hit (this section and
# any section still to come) renders as a one-line headline pointer instead
# of a full block. A hit's own block is never partially cut.
# ---------------------------------------------------------------------------


class TestMaxCharsBudget:
    def test_hit_beyond_budget_renders_as_headline_pointer(self) -> None:
        hit1 = _h("e1", "episodic", snippet="first hit")
        hit2 = _h("e2", "episodic", snippet="second hit", score=0.9)
        # Exactly enough budget for hit1 alone (header + its block) —
        # derived from the real renderer so this test doesn't hard-code
        # marker/joiner formatting.
        budget = len(render_sectioned([hit1]))

        out = render_sectioned([hit1, hit2], max_chars=budget)

        assert "first hit" in out
        assert "- second hit (e2; drill for the body)" in out
        # hit2 never got a full FRAGMENT block of its own.
        assert out.count("=== FRAGMENT:") == 1

    def test_headline_pointer_falls_back_to_uri_when_snippet_empty(self) -> None:
        hit1 = _h("e1", "episodic", snippet="keeps this one whole")
        hit2 = _h("e2", "episodic", snippet="", score=0.9)
        budget = len(render_sectioned([hit1]))

        out = render_sectioned([hit1, hit2], max_chars=budget)

        assert "- e2 (e2; drill for the body)" in out

    def test_never_truncates_a_single_oversized_block(self) -> None:
        """A block that alone would blow the budget is never partially
        cut — it is either rendered whole (the floor guarantee, first
        block only) or replaced entirely by a pointer (every later
        block), never a partial/cut slice of its body."""
        first = _h("e1", "episodic", snippet="first headline",
                    summary="a" * 3000)
        second = _h("e2", "episodic", snippet="second headline",
                     summary="b" * 3000, score=0.9)

        out = render_sectioned([first, second], max_chars=50)

        assert ("a" * 3000) in out
        assert ("b" * 3000) not in out
        assert "- second headline (e2; drill for the body)" in out

    def test_floor_renders_the_first_block_whole_even_over_budget(self) -> None:
        """The highest-ranked block always renders whole, even alone it
        exceeds `max_chars` — a rendering never carries zero content.
        The budget applies from the second block on."""
        huge = _h("e1", "episodic", snippet="huge first", summary="s" * 3000)
        small = _h("e2", "episodic", snippet="small second", score=0.9)

        out = render_sectioned([huge, small], max_chars=50)

        assert ("s" * 3000) in out
        assert "- small second (e2; drill for the body)" in out
        assert out.count("=== FRAGMENT:") == 1

    def test_floor_applies_across_sections_to_the_very_first_hit_only(self) -> None:
        """The floor is a property of the whole rendering, not per
        section: only the first hit overall (canonical, since it sorts
        first in `_SECTION_ORDER`) gets the exemption. A later section's
        oversized hit still degrades to a pointer."""
        huge_canonical = _h(
            "person:m", "entity", snippet="marcelo", summary="c" * 3000,
        )
        huge_fragment = _h(
            "e1", "episodic", snippet="a fragment", summary="f" * 3000,
            score=0.9,
        )

        out = render_sectioned(
            [huge_canonical, huge_fragment], max_chars=50,
        )

        assert ("c" * 3000) in out
        assert ("f" * 3000) not in out
        assert "- a fragment (e1; drill for the body)" in out

    def test_budget_ratchet_carries_across_sections(self) -> None:
        """Once the budget trips in one section, hits in a LATER section
        also degrade to pointers, even though individually small —
        the config field docs this as a one-way switch, not a per-hit
        re-check."""
        canonical_hit = _h("person:m", "entity", snippet="marcelo")
        fragment_hit = _h("e1", "episodic", snippet="a small fragment")
        budget = len(render_sectioned([canonical_hit]))

        out = render_sectioned([canonical_hit, fragment_hit], max_chars=budget)

        assert "marcelo" in out
        assert "=== FRAGMENT:" not in out
        assert "- a small fragment (e1; drill for the body)" in out
        # The section header still appears even though every hit in it
        # is a pointer — structure/order stays intact for the LLM.
        assert "## Fragment" in out

    def test_twelve_hits_all_represented_under_tight_budget(self) -> None:
        """Task 2 acceptance scenario: many ~1000-char hits, a 4000-char
        budget — every hit still appears (full block or pointer), not
        all of them get full blocks, and the full-block portion respects
        the budget (pointer lines are cheap and allowed beyond it)."""
        hits = [
            _h(f"e{i}", "episodic", snippet=f"headline {i}",
               score=1.0 - i * 0.01, summary="x" * 1000)
            for i in range(12)
        ]

        out = render_sectioned(hits, max_chars=4000)

        for i in range(12):
            assert f"e{i}" in out
        assert "drill for the body" in out
        assert out.count("=== FRAGMENT:") < 12
        assert len(out) < 4000 + 12 * 80
