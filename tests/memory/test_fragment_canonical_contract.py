"""§2.H — fragment/canonical retrieval contract.

The doc 18 §6 promise — "página consolidada y entries post-cursor
coexisten en los resultados; el LLM reconcilia en read-time con
timestamps y contexto" — was a design intent that did not survive
into the LLM delivery contract until §2.H. These tests pin the
contract on both delivery paths (lazy `memory_search` + eager
`hot_layer`) so future changes don't silently regress it.

Marker convention follows the compaction precedent
(``=== ARCHIVED SUMMARY ===`` per bitácora 2026-05-19) and was
formalised in ``docs/memory/06_prompts_and_instructions.md`` §8.3 to:

- ``=== CANONICAL: <ref> (consolidated <ts>) === ... === END CANONICAL ===``
- ``=== FRAGMENT: <path> (ts <ts>) === ... === END FRAGMENT ===``
"""

from __future__ import annotations

import asyncio
import datetime
import tempfile
from pathlib import Path

import pytest

from durin.memory.aliases_cache import _clear_all
from durin.memory.entity_page import EntityPage
from durin.memory.hot_layer import read_hot_layer
from durin.memory.search import Result, search_dreamed
from durin.memory.store import store_memory


@pytest.fixture(autouse=True)
def _isolate_cache() -> None:
    _clear_all()
    yield
    _clear_all()


# ---------------------------------------------------------------------------
# Result schema additions
# ---------------------------------------------------------------------------


class TestResultKind:
    def test_canonical_kind_when_entity_page(self) -> None:
        r = Result(
            source="memory", uri="memory/entity_page/person:m",
            headline="M", snippet="m", class_name="entity_page",
        )
        assert r.kind == "canonical"

    def test_fragment_kind_when_episodic_class(self) -> None:
        r = Result(
            source="memory", uri="memory/episodic/x",
            headline="x", snippet="x", class_name="episodic",
        )
        assert r.kind == "fragment"

    def test_fragment_kind_for_stable_corpus_pending(self) -> None:
        for cls in ("stable", "corpus", "pending"):
            r = Result(
                source="memory", uri=f"memory/{cls}/x",
                headline="x", snippet="x", class_name=cls,
            )
            assert r.kind == "fragment", f"class_name={cls!r}"

    def test_session_kind_for_session_source(self) -> None:
        r = Result(source="sessions", uri="sessions/k.md", headline="s", snippet="s")
        assert r.kind == "session"

    def test_ingested_kind_for_ingested_source(self) -> None:
        r = Result(source="ingested", uri="ingested/abc/source.md", headline="i", snippet="i")
        assert r.kind == "ingested"


class TestToDictContract:
    def test_includes_kind_always(self) -> None:
        r = Result(
            source="memory", uri="memory/entity_page/p:m",
            headline="h", snippet="s", class_name="entity_page",
        )
        d = r.to_dict()
        assert d["kind"] == "canonical"

    def test_includes_new_fields_when_present(self) -> None:
        r = Result(
            source="memory", uri="memory/episodic/e1",
            headline="h", snippet="s",
            class_name="episodic", valid_from="2026-05-23",
            entities=("person:marcelo", "project:durin"),
        )
        d = r.to_dict()
        assert d["class_name"] == "episodic"
        assert d["valid_from"] == "2026-05-23"
        assert d["entities"] == ["person:marcelo", "project:durin"]

    def test_omits_new_fields_when_empty(self) -> None:
        r = Result(source="sessions", uri="sessions/k.md", headline="h", snippet="s")
        d = r.to_dict()
        assert "class_name" not in d
        assert "valid_from" not in d
        assert "entities" not in d


# ---------------------------------------------------------------------------
# render_block — the LLM-facing marker format
# ---------------------------------------------------------------------------


class TestRenderBlock:
    def test_canonical_header_and_footer(self) -> None:
        r = Result(
            source="memory", uri="memory/entity_page/person:marcelo",
            headline="Marcelo", snippet="m",
            summary="Marcelo Marmol",
            class_name="entity_page",
            entities=("person:marcelo",),
        )
        out = r.render_block()
        assert out.startswith("=== CANONICAL: memory/entity_page/person:marcelo (canonical entity page) ===")
        assert out.endswith("=== END CANONICAL ===")
        assert "Marcelo Marmol" in out

    def test_fragment_includes_timestamp(self) -> None:
        r = Result(
            source="memory", uri="memory/episodic/e1",
            headline="h", snippet="s",
            summary="Just observed something",
            class_name="episodic",
            valid_from="2026-05-23T18:00",
            entities=("person:marcelo",),
        )
        out = r.render_block()
        assert "=== FRAGMENT: memory/episodic/e1 (ts: 2026-05-23T18:00) ===" in out
        assert "Just observed something" in out
        assert "Entities: person:marcelo" in out
        assert out.endswith("=== END FRAGMENT ===")

    def test_fragment_with_no_valid_from_still_renders(self) -> None:
        r = Result(
            source="memory", uri="memory/stable/x",
            headline="h", snippet="s", class_name="stable",
        )
        out = r.render_block()
        # No ts hint and no canonical marker since stable is fragment.
        assert out.startswith("=== FRAGMENT: memory/stable/x ===")


# ---------------------------------------------------------------------------
# search_dreamed: grep path surfaces entities + populates fields
# ---------------------------------------------------------------------------


class TestSearchDreamedGrep:
    def test_entry_carries_fields_for_llm(self, tmp_path: Path) -> None:
        store_memory(
            tmp_path, content="Marcelo prefers pytest",
            entities=["person:marcelo", "topic:pytest"],
            valid_from=datetime.date(2026, 5, 23),
        )
        hits = search_dreamed(tmp_path, "marcelo")
        # Filter to the episodic entry (entity pages also surface now).
        frags = [r for r in hits if r.kind == "fragment"]
        assert len(frags) == 1
        f = frags[0]
        assert f.class_name == "episodic"
        assert f.valid_from == "2026-05-23"
        assert set(f.entities) == {"person:marcelo", "topic:pytest"}

    def test_entity_page_surfaces_as_canonical(self, tmp_path: Path) -> None:
        page = EntityPage(type="person", name="Marcelo Marmol", aliases=["Marcelo"])
        page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")

        hits = search_dreamed(tmp_path, "marcelo")
        cans = [r for r in hits if r.kind == "canonical"]
        assert len(cans) == 1
        c = cans[0]
        assert c.class_name == "entity_page"
        assert c.uri == "memory/entity_page/person:marcelo"
        assert c.entities == ("person:marcelo",)

    def test_canonical_and_fragment_coexist(self, tmp_path: Path) -> None:
        """doc 18 §6 promise: ambos en los resultados."""
        page = EntityPage(type="person", name="Marcelo", aliases=["marcelo"])
        page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
        store_memory(
            tmp_path, content="marcelo update",
            entities=["person:marcelo"],
            valid_from=datetime.date(2026, 5, 23),
        )
        hits = search_dreamed(tmp_path, "marcelo")
        kinds = {r.kind for r in hits}
        assert kinds == {"canonical", "fragment"}

    def test_entity_page_under_archive_is_skipped(self, tmp_path: Path) -> None:
        """Absorbed pages live in <canonical_slug>/archive/ and must
        NOT leak into normal retrieval — they're reachable only via
        ``durin memory expand``."""
        canonical = EntityPage(type="person", name="Marcelo", aliases=["m"])
        canonical.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
        archived = EntityPage(type="person", name="Marcelo M", aliases=["m"])
        archived.save(
            tmp_path / "memory" / "entities" / "person" / "marcelo" / "archive" / "marcelo_m.md"
        )

        hits = search_dreamed(tmp_path, "marcelo")
        canonical_uris = {r.uri for r in hits if r.kind == "canonical"}
        # Only the top-level canonical, not the archived one.
        assert canonical_uris == {"memory/entity_page/person:marcelo"}


# ---------------------------------------------------------------------------
# memory_search tool — `rendered` field at the LLM boundary
# ---------------------------------------------------------------------------


def test_memory_search_tool_includes_rendered_blocks(tmp_path: Path) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    page = EntityPage(type="person", name="Marcelo", aliases=["marcelo"])
    page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    store_memory(
        tmp_path, content="marcelo recent observation",
        entities=["person:marcelo"],
        valid_from=datetime.date(2026, 5, 23),
    )

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="marcelo", scope="dreamed", level="warm"))
    assert out["total"] >= 2
    for r in out["results"]:
        assert "rendered" in r, "tool boundary must add the marker block"
        assert r["rendered"].startswith("=== ")
        assert r["rendered"].endswith(" ===")


# ---------------------------------------------------------------------------
# hot_layer — eager delivery path
# ---------------------------------------------------------------------------


class TestHotLayerCanonicalSection:
    def test_canonical_entity_page_appears_in_render(self, tmp_path: Path) -> None:
        page = EntityPage(
            type="person", name="Marcelo Marmol",
            aliases=["Marcelo", "mmarmol"],
            body="Prefers pytest.",
            extra={"identifiers": {"email": ["mmarmol@mxhero.com"]}},
        )
        page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")

        rendered = read_hot_layer(tmp_path).render()
        assert "## Memory: Canonical pages" in rendered
        assert "=== CANONICAL: person:marcelo" in rendered
        assert "Marcelo Marmol" in rendered
        assert "mmarmol@mxhero.com" in rendered
        assert "Prefers pytest." in rendered
        assert "=== END CANONICAL ===" in rendered

    def test_archive_pages_excluded_from_hot_layer(self, tmp_path: Path) -> None:
        canonical = EntityPage(type="person", name="Marcelo", aliases=["m"])
        canonical.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
        archived = EntityPage(type="person", name="Old Marcelo", aliases=["m"])
        archived.save(
            tmp_path / "memory" / "entities" / "person" / "marcelo" / "archive" / "marcelo_m.md"
        )

        rendered = read_hot_layer(tmp_path).render()
        assert "Old Marcelo" not in rendered


class TestHotLayerFragmentsSection:
    def test_post_cursor_fragment_appears(self, tmp_path: Path) -> None:
        page = EntityPage(
            type="person", name="Marcelo", aliases=["m"],
            dream_processed_through="2026-05-20T00:00:00",
        )
        page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
        store_memory(
            tmp_path, content="post-cursor obs",
            entities=["person:marcelo"],
            valid_from=datetime.date(2026, 5, 22),
        )

        rendered = read_hot_layer(tmp_path).render()
        assert "## Memory: Recent fragments (post-cursor)" in rendered
        # Marker format (doc 06 §8.3): `=== FRAGMENT: <path> (ts <ts>) ===`
        # — path is workspace-relative, not the entity ref.
        assert "=== FRAGMENT: memory/episodic/" in rendered
        assert "(ts 2026-05-22" in rendered
        assert "post-cursor obs" in rendered

    def test_pre_cursor_fragment_filtered_out(self, tmp_path: Path) -> None:
        page = EntityPage(
            type="person", name="Marcelo", aliases=["m"],
            dream_processed_through="2026-05-20T00:00:00",
        )
        page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
        # Pre-cursor entry → already in the canonical, must not appear
        # in the fragments section.
        store_memory(
            tmp_path, content="ancient observation",
            entities=["person:marcelo"],
            valid_from=datetime.date(2026, 5, 10),
        )

        layer = read_hot_layer(tmp_path)
        rendered = layer.render()
        # The legacy "Key Points" section still surfaces all headlines —
        # that's not the §2.H curated path. The fragments section is
        # the §2.H path and MUST NOT include the pre-cursor entry.
        if layer.fragment_blocks:
            joined = "\n".join(layer.fragment_blocks)
            assert "ancient observation" not in joined


def test_hot_layer_falls_back_gracefully_with_no_entities_dir(tmp_path: Path) -> None:
    """Cold workspace renders without the canonical/fragment sections,
    but identity + legacy classes still work."""
    (tmp_path / "memory" / "stable").mkdir(parents=True)
    (tmp_path / "memory" / "stable" / "IDENTITY.md").write_text("Durin agent.")
    store_memory(tmp_path, content="legacy entry", entities=[])

    rendered = read_hot_layer(tmp_path).render()
    assert "Durin agent" in rendered
    assert "## Memory: Canonical pages" not in rendered
    assert "## Memory: Recent fragments" not in rendered
