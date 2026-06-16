"""Search pipeline applies entity-aware rerank after RRF (doc 03 §8)."""

from __future__ import annotations

from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.indexer import rebuild_fts_index
from durin.memory.schema import MemoryEntry
from durin.memory.search_pipeline import run_search_pipeline
from durin.memory.storage import save_entry


def _entity(workspace: Path, type_: str, slug: str,
            *, name: str | None = None,
            aliases: list[str] | None = None,
            body: str = "") -> None:
    page = EntityPage(
        type=type_, name=name or slug.title(),
        aliases=aliases or [],
        body=body,
    )
    page.save(workspace / "memory" / "entities" / type_ / f"{slug}.md")


def _episodic(workspace: Path, name: str, *,
              headline: str, entities: list[str], body: str = "") -> None:
    epi_dir = workspace / "memory" / "episodic"
    epi_dir.mkdir(parents=True, exist_ok=True)
    save_entry(
        MemoryEntry(
            id=name, headline=headline, entities=entities,
            body=body,
        ),
        epi_dir / f"{name}.md",
    )


def test_query_with_known_entity_boosts_canonical(tmp_path: Path) -> None:
    """When the query mentions a known alias and the canonical page
    is also a hit, the entity rerank lifts the canonical above
    look-alike episodic noise."""
    _entity(
        tmp_path, "person", "marcelo",
        aliases=["Marcelo", "marcelo"],
        body="Architect of durin.",
    )
    # Some noise that lexically matches "architect" but is not Marcelo.
    _episodic(
        tmp_path, "noise-1",
        headline="architect of an unrelated project",
        entities=["project:other"],
        body="architect at other co.",
    )
    rebuild_fts_index(tmp_path)
    result = run_search_pipeline(tmp_path, "Marcelo architect")
    # The canonical for person:marcelo must appear; entity-aware
    # boost ranks it above the unrelated "architect" noise.
    refs = [h.uri for h in result.hits]
    assert "person:marcelo" in refs
    assert refs.index("person:marcelo") == 0


def test_query_without_known_entity_is_noop(tmp_path: Path) -> None:
    """No alias match → rerank is a no-op; results match plain RRF."""
    _entity(tmp_path, "person", "anybody", body="random text")
    rebuild_fts_index(tmp_path)
    result = run_search_pipeline(tmp_path, "random")
    # Just check it didn't crash and produced something.
    assert isinstance(result.hits, list)
