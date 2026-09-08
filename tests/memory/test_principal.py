from datetime import datetime, timezone

from durin.memory.distill_dream import outline_path_for
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.principal import (
    _MAX_LIBRARY_DOCS,
    ANONYMOUS,
    _library_subjects,
    build_library_awareness,
    build_pinned_context,
    ensure_owner,
    list_always_on,
    mark_always_on,
    resolve_principal,
)
from durin.memory.reference import ingest_reference

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def test_library_awareness_empty_workspace(tmp_path):
    assert build_library_awareness(tmp_path) == ""


def test_library_awareness_lists_docs_with_outline_abstract(tmp_path):
    import json

    ingest_reference(tmp_path, "The Durin Handbook", "# H\n\nbody.\n")
    ingest_reference(tmp_path, "Thinking Fast and Slow", "# T\n\nbody.\n")
    # distil one → its abstract becomes the one-liner
    outline_path_for(tmp_path, "thinking-fast-and-slow").write_text(
        json.dumps({"abstract": "Kahneman on two systems of thought.", "chunk_count": 1})
    )

    block = build_library_awareness(tmp_path, abstracts=True)
    assert "## Your document library (2 documents)" in block
    assert 'scope="library"' in block
    assert "- The Durin Handbook" in block            # title-only (not distilled)
    assert "- Thinking Fast and Slow — Kahneman on two systems of thought." in block


def _derived(slug: str) -> FieldPatch:
    return FieldPatch(kind="derived_from", value=f"reference:{slug}",
                      author="dream", source_ref="s", at=NOW)


def test_library_awareness_no_subjects_map_below_cap(tmp_path):
    ingest_reference(tmp_path, "A Book", "# a\n\nx.\n")
    ingest_reference(tmp_path, "B Book", "# b\n\ny.\n")
    write_entity(tmp_path, "topic:x", [_derived("a-book")], create=True, name="X")
    block = build_library_awareness(tmp_path)
    # Nothing hidden past the cap → the subjects map is redundant, so omitted.
    assert "Covers:" not in block


def test_library_awareness_subjects_map_activates_when_capped(tmp_path):
    for i in range(4):
        ingest_reference(tmp_path, f"Doc {i}", f"# d{i}\n\nbody.\n")
    # a dream-distilled subject shared across two documents
    write_entity(tmp_path, "topic:paraprostatic-cysts",
                 [_derived("doc-0"), _derived("doc-1")],
                 create=True, name="Paraprostatic cysts")
    block = build_library_awareness(tmp_path, max_docs=2)
    assert "Covers: " in block
    covers_line = block.split("Covers:")[1].splitlines()[0]
    assert "Paraprostatic cysts" in covers_line
    assert "…and 2 more" in block            # hidden docs reachable by subject


def test_library_subjects_excludes_agent_linked_and_ranks_by_breadth(tmp_path):
    for i in range(3):
        ingest_reference(tmp_path, f"Doc {i}", f"# d{i}\n\nbody.\n")
    # broad subject (2 docs, dream) → must rank first
    write_entity(tmp_path, "topic:uroperitoneum",
                 [_derived("doc-0"), _derived("doc-1")],
                 create=True, name="Uroperitoneum")
    # narrow subject (1 doc, dream)
    write_entity(tmp_path, "topic:creatinine", [_derived("doc-2")],
                 create=True, name="Creatinine")
    # agent-linked patient (must be EXCLUDED — the doc isn't about it)
    write_entity(tmp_path, "patient:rex",
                 [FieldPatch(kind="derived_from", value="reference:doc-0",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Rex")
    subjects = _library_subjects(tmp_path)
    assert "Rex" not in subjects
    assert subjects[0] == "Uroperitoneum"    # broadest first
    assert "Creatinine" in subjects


def test_library_awareness_caps_and_notes_overflow(tmp_path):
    for i in range(_MAX_LIBRARY_DOCS + 5):
        ingest_reference(tmp_path, f"Doc {i:03d}", f"# D{i}\n\nbody.\n")
    block = build_library_awareness(tmp_path)
    assert f"({_MAX_LIBRARY_DOCS + 5} documents)" in block
    assert "- …and 5 more" in block
    # exactly max_docs listed + the overflow note line
    assert block.count("\n- ") == _MAX_LIBRARY_DOCS + 1


def test_pinned_context_includes_library_awareness(tmp_path):
    ensure_owner(tmp_path, "person:marcelo", name="Marcelo")
    ingest_reference(tmp_path, "A Book", "# B\n\nbody.\n")
    ctx = build_pinned_context(tmp_path, "person:marcelo")
    assert "Your document library" in ctx
    assert "A Book" in ctx


def test_resolve_principal_channel_then_owner_then_anonymous():
    cmap = {"slack:U1": "person:alex"}
    assert resolve_principal("slack:U1", owner="person:marcelo", channel_map=cmap) == "person:alex"
    assert resolve_principal("slack:U9", owner="person:marcelo", channel_map=cmap) == "person:marcelo"
    assert resolve_principal(None) == ANONYMOUS


def test_ensure_owner_cold_start(tmp_path):
    created = ensure_owner(tmp_path, "person:marcelo", name="Marcelo")
    assert created is True
    assert (tmp_path / "memory/entities/person/marcelo.md").exists()
    assert ensure_owner(tmp_path, "person:marcelo") is False     # idempotent


def test_mark_and_list_always_on(tmp_path):
    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Respond in Spanish.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Always Spanish")
    assert list_always_on(tmp_path) == []
    mark_always_on(tmp_path, "practice:spanish")
    assert "practice:spanish" in list_always_on(tmp_path)


def test_build_pinned_context_includes_principal_and_always_on(tmp_path):
    ensure_owner(tmp_path, "person:marcelo", name="Marcelo")
    write_entity(tmp_path, "person:marcelo",
                 [FieldPatch(kind="body_append", value="Architect; prefers Spanish.",
                             author="agent", source_ref="s", at=NOW)])
    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Always respond in Spanish.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Always Spanish")
    mark_always_on(tmp_path, "practice:spanish")

    ctx = build_pinned_context(tmp_path, "person:marcelo")
    assert "Who you're talking to" in ctx
    assert "Marcelo" in ctx and "prefers Spanish" in ctx
    assert "Always-on guidance" in ctx
    assert "Always respond in Spanish." in ctx
    assert "<!--" not in ctx                       # provenance markers stripped


def test_library_awareness_prefers_curated_topic_index(tmp_path):
    import json as _json
    ingest_reference(tmp_path, "A Book", "# a\n\nx.\n")
    # a granular distilled subject the heuristic would otherwise surface
    write_entity(tmp_path, "topic:granular-thing", [_derived("a-book")],
                 create=True, name="Granular thing")
    # curated index present → clean theme labels, shown even below the cap
    (tmp_path / "memory" / "references" / "_topics.json").write_text(_json.dumps(
        {"topics": [{"label": "Clean Theme", "docs": ["a-book"]}],
         "signature": ["a-book:1"], "doc_count": 1}))
    block = build_library_awareness(tmp_path)
    assert "Covers: Clean Theme." in block
    assert "Granular thing" not in block   # curated index wins over the heuristic


def test_pinned_refs_is_principal_plus_always_on(tmp_path):
    from durin.memory.principal import pinned_refs

    ensure_owner(tmp_path, "person:marcelo", name="Marcelo")
    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Always respond in Spanish.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Always Spanish")
    write_entity(tmp_path, "practice:tdd",
                 [FieldPatch(kind="body_append", value="Tests first.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="TDD")
    mark_always_on(tmp_path, "practice:spanish")

    assert pinned_refs(tmp_path, "person:marcelo") == frozenset(
        {"person:marcelo", "practice:spanish"}
    )


def test_resolve_pinned_refs_never_raises_without_config(tmp_path):
    from durin.memory.principal import resolve_pinned_refs

    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Always respond in Spanish.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Always Spanish")
    mark_always_on(tmp_path, "practice:spanish")

    refs = resolve_pinned_refs(tmp_path)
    assert "practice:spanish" in refs
    assert "person:anonymous" in refs      # no owner configured in the test home


def test_pinned_block_carries_aliases_relations_and_sources(tmp_path):
    ensure_owner(tmp_path, "person:marcelo", name="Marcelo")
    write_entity(tmp_path, "person:marcelo", [
        FieldPatch(kind="body_append", value="Architect of durin.",
                   author="agent", source_ref="s", at=NOW),
        FieldPatch(kind="alias", value="marce", author="agent", source_ref="s", at=NOW),
        FieldPatch(kind="relation", value={"to": "project:durin", "type": "maintainer"},
                   author="agent", source_ref="s", at=NOW),
        FieldPatch(kind="derived_from", value="reference:durin-handbook",
                   author="dream", source_ref="s", at=NOW),
    ])

    ctx = build_pinned_context(tmp_path, "person:marcelo")

    assert "Aliases: marce." in ctx
    assert "maintainer project:durin" in ctx
    assert "Sources: reference:durin-handbook." in ctx


def test_resolve_pinned_refs_caches_within_ttl(tmp_path, monkeypatch):
    """The search dedup calls this on every search; the pinned set only moves
    when a dream flips always_on or the owner config changes, so repeated
    calls inside the TTL must not re-walk the entity tree."""
    from durin.memory import principal as principal_mod
    from durin.memory.principal import resolve_pinned_refs

    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Always respond in Spanish.",
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Always Spanish")
    mark_always_on(tmp_path, "practice:spanish")

    real = principal_mod.list_always_on
    calls = 0

    def _counting(workspace):
        nonlocal calls
        calls += 1
        return real(workspace)

    monkeypatch.setattr(principal_mod, "list_always_on", _counting)

    first = resolve_pinned_refs(tmp_path)
    assert "practice:spanish" in first
    assert resolve_pinned_refs(tmp_path) == first
    assert calls == 1

    assert resolve_pinned_refs(tmp_path, ttl_s=0) == first
    assert calls == 2


def test_library_awareness_is_titles_only_by_default(tmp_path):
    import json

    ingest_reference(tmp_path, "Thinking Fast and Slow", "# T\n\nbody.\n")
    outline_path_for(tmp_path, "thinking-fast-and-slow").write_text(
        json.dumps({"abstract": "Kahneman on two systems of thought.", "chunk_count": 1})
    )

    block = build_library_awareness(tmp_path)

    assert "- Thinking Fast and Slow" in block
    assert "Kahneman" not in block


def test_library_awareness_zero_docs_keeps_header_count_and_subjects(tmp_path):
    ingest_reference(tmp_path, "A Book", "# a\n\nx.\n")
    ingest_reference(tmp_path, "B Book", "# b\n\ny.\n")
    write_entity(tmp_path, "topic:x", [_derived("a-book")], create=True, name="X")

    block = build_library_awareness(tmp_path, max_docs=0)

    assert "## Your document library (2 documents)" in block
    assert "- A Book" not in block
    assert "…and 2 more" in block
    assert "Covers: X" in block


def test_library_awareness_reads_only_the_listed_documents(tmp_path, monkeypatch):
    from durin.memory import principal as p

    for i in range(5):
        ingest_reference(tmp_path, f"Doc {i}", f"# d{i}\n\nbody.\n")
    seen: list[str] = []
    real = p._doc_descriptor

    def _spy(workspace, slug, md_path, **kw):
        seen.append(slug)
        return real(workspace, slug, md_path, **kw)

    monkeypatch.setattr(p, "_doc_descriptor", _spy)
    build_library_awareness(tmp_path, max_docs=2)
    assert len(seen) == 2


def test_build_pinned_context_passes_library_caps(tmp_path):
    ingest_reference(tmp_path, "A Book", "# a\n\nx.\n")
    ingest_reference(tmp_path, "B Book", "# b\n\ny.\n")

    ctx = build_pinned_context(tmp_path, "person:marcelo", library_max_docs=1)

    assert "- A Book" in ctx
    assert "- B Book" not in ctx
    assert "…and 1 more" in ctx


def test_principal_body_is_capped_with_a_pointer_to_the_full_page(tmp_path):
    ensure_owner(tmp_path, "person:marcelo", name="Marcelo")
    long_body = " ".join(f"fact{i}" for i in range(600))  # ≈ 4 000 chars
    write_entity(tmp_path, "person:marcelo",
                 [FieldPatch(kind="body_append", value=long_body,
                             author="agent", source_ref="s", at=NOW),
                  FieldPatch(kind="relation", value={"to": "project:durin", "type": "maintainer"},
                             author="agent", source_ref="s", at=NOW)])

    ctx = build_pinned_context(tmp_path, "person:marcelo")

    who = ctx.split("## Always-on guidance")[0]
    assert len(who) < 1500 + 200
    assert "memory_read_entity person:marcelo" in who
    assert "fact599" not in who
    assert "maintainer project:durin" in who


def test_always_on_pins_are_not_capped_by_the_principal_rule(tmp_path):
    long_body = " ".join(f"rule{i}" for i in range(400))  # ≈ 2 800 chars
    write_entity(tmp_path, "practice:long",
                 [FieldPatch(kind="body_append", value=long_body,
                             author="agent", source_ref="s", at=NOW)],
                 create=True, name="Long practice")
    mark_always_on(tmp_path, "practice:long")

    ctx = build_pinned_context(tmp_path, "person:nobody")

    assert "rule399" in ctx
