"""Tests for `durin.memory.entity_rename` — key changes that redirect every
reference, and the merge redirect that shares the same rewrite."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.absorption import EntityAbsorption
from durin.memory.entity_page import EntityPage
from durin.memory.entity_rename import (
    EntityRenameError,
    collect_ref_rewrites,
    rename_entity,
)
from durin.memory.field_provenance import relation_prov_key
from durin.memory.refine_dream import (
    _pair_key,
    add_flagged,
    add_tombstone,
    read_flagged,
    read_tombstones,
)
from durin.memory.schema import MemoryEntry
from durin.memory.storage import load_entry, save_entry


def _page(ws: Path, type_: str, slug: str, *, name: str | None = None,
          aliases: list[str] | None = None, relations: list[dict] | None = None,
          provenance: dict | None = None, extra: dict | None = None,
          author: str = "agent_created") -> Path:
    page = EntityPage(type=type_, name=name or slug, aliases=aliases or [],
                      body=f"About {slug}.", relations=relations or [],
                      provenance=provenance or {}, extra=extra or {}, author=author)
    path = ws / "memory" / "entities" / type_ / f"{slug}.md"
    if extra and "archived_into" in extra:
        path = ws / "memory" / "archive" / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


def _load(ws: Path, ref: str) -> EntityPage:
    t, _, s = ref.partition(":")
    page = EntityPage.from_file(ws / "memory" / "entities" / t / f"{s}.md")
    assert page is not None
    return page


def _entry(ws: Path, id_: str, entities: list[str]) -> Path:
    path = ws / "memory" / "episodic" / f"{id_}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_entry(MemoryEntry(id=id_, headline="h", entities=entities, body="b"), path)
    return path


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    _page(tmp_path, "topic", "5e", name="5e", aliases=["D&D 5e"])
    _page(tmp_path, "topic", "dungeons-and-dragons", name="Dungeons & Dragons",
          relations=[{"to": "topic:5e", "type": "has_edition"}],
          provenance={"relations": {
              relation_prov_key("topic:5e", "has_edition"): {"author": "dream"}}})
    _page(tmp_path, "topic", "old-5e", name="old", extra={"archived_into": "topic:5e"})
    _entry(tmp_path, "e1", ["topic:5e", "person:gabriel"])
    return tmp_path


class TestRename:
    def test_moves_page_and_keeps_old_key_and_name_as_aliases(self, ws: Path) -> None:
        res = rename_entity(ws, "topic:5e", "dnd-5e", new_name="D&D 5ª edición")
        assert res.new_ref == "topic:dnd-5e"
        assert res.sha
        assert not (ws / "memory/entities/topic/5e.md").exists()
        page = _load(ws, "topic:dnd-5e")
        assert page.name == "D&D 5ª edición"
        assert "5e" in page.aliases  # old slug and old name ("5e") kept
        assert "D&D 5e" in page.aliases

    def test_redirects_relations_entries_and_archive_pointers(self, ws: Path) -> None:
        res = rename_entity(ws, "topic:5e", "dnd-5e")
        dnd = _load(ws, "topic:dungeons-and-dragons")
        assert dnd.relations == [{"to": "topic:dnd-5e", "type": "has_edition"}]
        rel_prov = dnd.provenance["relations"]
        assert relation_prov_key("topic:dnd-5e", "has_edition") in rel_prov
        assert relation_prov_key("topic:5e", "has_edition") not in rel_prov
        assert load_entry(ws / "memory/episodic/e1.md").entities == [
            "topic:dnd-5e", "person:gabriel"]
        arch = EntityPage.from_file(ws / "memory/archive/entities/topic/old-5e.md")
        assert arch.extra["archived_into"] == "topic:dnd-5e"
        assert set(res.rewritten) == {
            "entities/topic/dungeons-and-dragons.md",
            "episodic/e1.md",
            "archive/entities/topic/old-5e.md",
        }

    def test_entries_stay_outside_the_memory_git_history(self, ws: Path) -> None:
        from dulwich.repo import Repo
        res = rename_entity(ws, "topic:5e", "dnd-5e")
        repo = Repo(str(ws / "memory"))
        try:
            tree = repo[repo[res.sha.encode()].tree]
            assert b"episodic" not in {e.path for e in tree.items()}
        finally:
            repo.close()

    def test_side_stores_follow_the_new_key(self, ws: Path) -> None:
        add_tombstone(ws, "topic:5e", "topic:dungeons-and-dragons")
        add_flagged(ws, "topic:5e", "topic:other", verdict="unclear",
                    confidence=60, reasoning="r")
        rename_entity(ws, "topic:5e", "dnd-5e")
        assert read_tombstones(ws) == [("topic:dnd-5e", "topic:dungeons-and-dragons")]
        assert read_flagged(ws)[0]["pair"] == ["topic:dnd-5e", "topic:other"]

    def test_stored_proposals_follow_the_new_key(self, ws: Path) -> None:
        add_flagged(ws, "topic:5e", "topic:dungeons-and-dragons", verdict="related",
                    confidence=80, reasoning="r", proposal={
                        "kind": "relate", "survivor": None,
                        "renames": {"topic:5e": {"slug": "x", "name": None}},
                        "alias_moves": [{"alias": "D&D", "keep_on": "topic:5e"}],
                        "relation": {"from_ref": "topic:5e", "type": "edition_of",
                                     "to_ref": "topic:dungeons-and-dragons"}})
        rename_entity(ws, "topic:5e", "dnd-5e")
        prop = read_flagged(ws)[0]["proposal"]
        assert list(prop["renames"]) == ["topic:dnd-5e"]
        assert prop["alias_moves"][0]["keep_on"] == "topic:dnd-5e"
        assert prop["relation"]["from_ref"] == "topic:dnd-5e"

    def test_name_only_change_keeps_the_key(self, ws: Path) -> None:
        res = rename_entity(ws, "topic:5e", "5e", new_name="Quinta edición")
        assert res.new_ref == "topic:5e"
        page = _load(ws, "topic:5e")
        assert page.name == "Quinta edición"
        assert "5e" in page.aliases

    def test_noop_when_nothing_changes(self, ws: Path) -> None:
        assert rename_entity(ws, "topic:5e", "5e").sha is None

    @pytest.mark.parametrize("slug", ["", "Upper", "has space", "-lead", "a/b", "x" * 90])
    def test_rejects_bad_slugs(self, ws: Path, slug: str) -> None:
        with pytest.raises(EntityRenameError):
            rename_entity(ws, "topic:5e", slug)

    def test_rejects_a_live_key(self, ws: Path) -> None:
        with pytest.raises(EntityRenameError):
            rename_entity(ws, "topic:5e", "dungeons-and-dragons")

    def test_rejects_missing_page(self, ws: Path) -> None:
        with pytest.raises(EntityRenameError):
            rename_entity(ws, "topic:nope", "x")


class TestCollectRefRewrites:
    def test_collapses_duplicates_and_drops_self_edges(self, tmp_path: Path) -> None:
        _page(tmp_path, "topic", "a", relations=[
            {"to": "topic:old", "type": "related_to"},
            {"to": "topic:new", "type": "related_to"},
        ])
        _page(tmp_path, "topic", "new", relations=[{"to": "topic:old", "type": "x"}])
        out = collect_ref_rewrites(tmp_path, "topic:old", "topic:new")
        a = EntityPage.from_text(out["entities/topic/a.md"].decode())
        assert a.relations == [{"to": "topic:new", "type": "related_to"}]
        new = EntityPage.from_text(out["entities/topic/new.md"].decode())
        assert new.relations == []

    def test_untouched_files_are_not_returned(self, tmp_path: Path) -> None:
        _page(tmp_path, "topic", "a", relations=[{"to": "topic:old-thing", "type": "x"}])
        assert collect_ref_rewrites(tmp_path, "topic:old", "topic:new") == {}


class TestAbsorbRedirect:
    def test_absorb_points_inbound_references_at_the_canonical(self, tmp_path: Path) -> None:
        _page(tmp_path, "topic", "dnd", name="D&D")
        _page(tmp_path, "topic", "d-d", name="D and D",
              relations=[{"to": "topic:dnd", "type": "same_as"}])
        _page(tmp_path, "topic", "campaign",
              relations=[{"to": "topic:d-d", "type": "uses"}])
        _entry(tmp_path, "e1", ["topic:d-d"])
        add_tombstone(tmp_path, "topic:d-d", "topic:other")
        EntityAbsorption(tmp_path).absorb("topic:dnd", "topic:d-d", reason="t")
        assert _load(tmp_path, "topic:campaign").relations == [
            {"to": "topic:dnd", "type": "uses"}]
        # the edge between the merged pages would be a self-edge: dropped
        assert _load(tmp_path, "topic:dnd").relations == []
        assert load_entry(tmp_path / "memory/episodic/e1.md").entities == ["topic:dnd"]
        # the absorbed page's decision follows it, and stays under the old key
        # too so an unmerge still has it
        assert read_tombstones(tmp_path) == [("topic:d-d", "topic:other"),
                                             ("topic:dnd", "topic:other")]
        assert _pair_key("topic:dnd", "topic:other") == "topic:dnd|topic:other"


class TestArchivedKeys:
    def test_reclaims_its_own_former_key_from_the_archive(self, ws: Path) -> None:
        # topic:old-5e was merged into topic:5e: 5e may take that key back.
        res = rename_entity(ws, "topic:5e", "old-5e")
        assert res.new_ref == "topic:old-5e"
        assert _load(ws, "topic:old-5e").name == "5e"
        moved = ws / "memory/archive/entities/topic/old-5e_archived.md"
        assert moved.exists()
        assert EntityPage.from_file(moved).extra["archived_into"] == "topic:old-5e"

    def test_an_archived_key_of_another_identity_stays_taken(self, tmp_path: Path) -> None:
        _page(tmp_path, "topic", "a", name="a")
        _page(tmp_path, "topic", "b", name="b")
        _page(tmp_path, "topic", "gone", name="gone", extra={"archived_into": "topic:b"})
        with pytest.raises(EntityRenameError):
            rename_entity(tmp_path, "topic:a", "gone")

    def test_follows_an_old_merge_chain(self, ws: Path) -> None:
        # dnd-5e was merged into d-d, which was later merged into 5e (an older
        # merge that did not redirect the first pointer).
        _page(ws, "topic", "d-d", name="d-d", extra={"archived_into": "topic:5e"})
        _page(ws, "topic", "dnd-5e", name="x", extra={"archived_into": "topic:d-d"})
        assert rename_entity(ws, "topic:5e", "dnd-5e").new_ref == "topic:dnd-5e"
