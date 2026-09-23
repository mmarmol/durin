"""Tests for `durin.memory.pair_resolution` — what to do with a colliding pair."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.pair_resolution import (
    AliasMove,
    RelationSpec,
    RenameSpec,
    Resolution,
    ResolutionError,
    apply_resolution,
    resolution_from_judge,
    validate_resolution,
)
from durin.memory.refine_dream import add_flagged, read_flagged, read_tombstones

A, B = "topic:5e", "topic:dungeons-and-dragons"


def _page(ws: Path, ref: str, *, name: str, aliases: list[str],
          relations: list[dict] | None = None) -> EntityPage:
    t, _, s = ref.partition(":")
    page = EntityPage(type=t, name=name, aliases=aliases, body=f"{name}.",
                      relations=relations or [], author="agent_created")
    page.save(ws / "memory" / "entities" / t / f"{s}.md")
    return page


def _load(ws: Path, ref: str) -> EntityPage:
    t, _, s = ref.partition(":")
    page = EntityPage.from_file(ws / "memory" / "entities" / t / f"{s}.md")
    assert page is not None
    return page


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    _page(tmp_path, A, name="5e", aliases=["D&D", "juego D&D original", "D&D 5e"])
    _page(tmp_path, B, name="Dungeons & Dragons", aliases=["D&D", "DUNGEONSCDRAGONS"])
    return tmp_path


def _pages(ws: Path) -> tuple[EntityPage, EntityPage]:
    return _load(ws, A), _load(ws, B)


class TestFromDict:
    def test_accepts_the_judge_shapes(self) -> None:
        r = Resolution.from_dict({
            "kind": "relate",
            "renames": {A: "dnd-5e"},
            "alias_moves": [{"alias": "D&D", "to": B}],
            "relation": {"from": A, "type": "edition of", "to": B},
        })
        assert r.renames[A] == RenameSpec(slug="dnd-5e")
        assert r.alias_moves == [AliasMove(alias="D&D", keep_on=B)]
        assert r.relation == RelationSpec(from_ref=A, type="edition of", to_ref=B)

    def test_round_trips(self) -> None:
        r = Resolution(kind="merge", survivor=B, renames={B: RenameSpec(slug="dnd")},
                       confidence=90, reasoning="r", source="tier2")
        assert Resolution.from_dict(r.to_dict()) == r


class TestFromJudge:
    def test_same_is_a_merge_into_ref_a_by_default(self) -> None:
        r = resolution_from_judge("same", None, A, B, confidence=97)
        assert (r.kind, r.survivor, r.confidence) == ("merge", A, 97)

    def test_related_without_an_edge_falls_back(self) -> None:
        assert resolution_from_judge("related", None, A, B).kind == "keep"
        r = resolution_from_judge("related", {"alias_moves": [{"alias": "x", "keep_on": A}]}, A, B)
        assert r.kind == "disambiguate"

    def test_different_is_keep_unless_it_changes_something(self) -> None:
        assert resolution_from_judge("different", {}, A, B).kind == "keep"
        r = resolution_from_judge("different", {"alias_moves": [{"alias": "D&D", "keep_on": B}]}, A, B)
        assert r.kind == "disambiguate"

    def test_unclear_keeps_only_an_explicit_proposal(self) -> None:
        assert resolution_from_judge("unclear", None, A, B) is None
        r = resolution_from_judge("unclear", {"kind": "relate",
                                              "relation": {"from": A, "type": "x", "to": B}}, A, B)
        assert r.kind == "relate"

    def test_malformed_proposal_degrades_to_the_verdict(self) -> None:
        assert resolution_from_judge("same", "nope", A, B).kind == "merge"  # type: ignore[arg-type]


class TestValidate:
    def test_normalizes_refs_types_and_drops_unknown_aliases(self, ws: Path) -> None:
        pa, pb = _pages(ws)
        res = Resolution(kind="relate",
                         alias_moves=[AliasMove("d&d", "b"), AliasMove("Nope", A)],
                         relation=RelationSpec("5e", "Edition Of", "b"))
        out = validate_resolution(res, A, B, pa, pb)
        assert out.alias_moves == [AliasMove("d&d", B)]
        assert out.relation == RelationSpec(A, "edition_of", B)

    @pytest.mark.parametrize("res", [
        Resolution(kind="bogus"),
        Resolution(kind="merge", survivor="topic:other"),
        Resolution(kind="relate"),
        Resolution(kind="relate", relation=RelationSpec(A, "x", A)),
        Resolution(kind="relate", relation=RelationSpec(A, "!!", B)),
        Resolution(kind="keep", renames={A: RenameSpec(slug="Bad Slug")}),
        Resolution(kind="keep", renames={A: RenameSpec(slug="dungeons-and-dragons")}),
        Resolution(kind="keep", renames={A: RenameSpec(slug="x"), B: RenameSpec(slug="x")}),
        Resolution(kind="disambiguate", alias_moves=[AliasMove("D&D", "topic:zzz")]),
    ])
    def test_rejects(self, ws: Path, res: Resolution) -> None:
        pa, pb = _pages(ws)
        with pytest.raises(ResolutionError):
            validate_resolution(res, A, B, pa, pb)

    def test_merge_only_renames_the_survivor(self, ws: Path) -> None:
        pa, pb = _pages(ws)
        res = Resolution(kind="merge", survivor=B,
                         renames={A: RenameSpec(slug="x"), B: RenameSpec(slug="dnd")})
        out = validate_resolution(res, A, B, pa, pb)
        assert out.renames == {B: RenameSpec(slug="dnd")}


class TestApply:
    def test_relate_moves_aliases_adds_the_edge_and_renames(self, ws: Path) -> None:
        add_flagged(ws, A, B, verdict="different", confidence=70, reasoning="r")
        res = Resolution(
            kind="relate",
            renames={A: RenameSpec(slug="dnd-5e", name="D&D 5ª edición")},
            alias_moves=[AliasMove("D&D", B), AliasMove("juego D&D original", B),
                         AliasMove("DUNGEONSCDRAGONS", "none")],
            relation=RelationSpec(A, "edition_of", B),
        )
        out = apply_resolution(ws, res, A, B, actor="user")
        assert out.refs == {A: "topic:dnd-5e", B: B}
        edition = _load(ws, "topic:dnd-5e")
        game = _load(ws, B)
        assert "D&D" not in edition.aliases and "juego D&D original" not in edition.aliases
        assert "5e" in edition.aliases  # old key kept as an alias
        assert "juego D&D original" in game.aliases and "D&D" in game.aliases
        assert "DUNGEONSCDRAGONS" not in game.aliases
        assert edition.relations == [{"to": B, "type": "edition_of"}]
        prov = next(iter(edition.provenance["relations"].values()))
        assert prov["author"] == "user"
        # the user decided they are different: tombstoned under the new key
        assert read_tombstones(ws) == [("topic:dnd-5e", B)]
        assert read_flagged(ws) == []

    def test_dream_resolution_leaves_no_tombstone(self, ws: Path) -> None:
        res = Resolution(kind="disambiguate", alias_moves=[AliasMove("D&D", B)])
        apply_resolution(ws, res, A, B, actor="dream")
        assert read_tombstones(ws) == []
        assert "D&D" not in _load(ws, A).aliases

    def test_keep_only_tombstones(self, ws: Path) -> None:
        before = _pages(ws)
        out = apply_resolution(ws, Resolution(kind="keep"), A, B, actor="user")
        assert out.commits == []
        assert _pages(ws) == before
        assert read_tombstones(ws) == [(A, B)]

    def test_merge_into_the_chosen_survivor_with_a_clearer_key(self, ws: Path) -> None:
        res = Resolution(kind="merge", survivor=B, renames={B: RenameSpec(slug="dnd")})
        out = apply_resolution(ws, res, A, B, actor="user")
        assert out.refs == {A: "topic:dnd", B: "topic:dnd"}
        assert not (ws / "memory/entities/topic/5e.md").exists()
        merged = _load(ws, "topic:dnd")
        assert merged.name == "Dungeons & Dragons"
        assert "5e" in merged.aliases
        assert read_tombstones(ws) == []

    def test_allow_rename_false_keeps_keys(self, ws: Path) -> None:
        res = Resolution(kind="disambiguate", renames={A: RenameSpec(slug="dnd-5e")},
                         alias_moves=[AliasMove("D&D", B)])
        out = apply_resolution(ws, res, A, B, actor="dream", allow_rename=False)
        assert out.refs == {A: A, B: B}
        assert (ws / "memory/entities/topic/5e.md").exists()


def test_a_taken_key_rejects_the_whole_resolution_before_writing(ws: Path) -> None:
    _page(ws, "topic:dnd-5e", name="other", aliases=[])
    res = Resolution(kind="relate", renames={A: RenameSpec(slug="dnd-5e")},
                     alias_moves=[AliasMove("D&D", B)],
                     relation=RelationSpec(A, "edition_of", B))
    before = _pages(ws)
    with pytest.raises(ResolutionError):
        apply_resolution(ws, res, A, B, actor="user")
    assert _pages(ws) == before


def test_rename_does_not_bring_back_an_alias_the_resolution_moved(ws: Path) -> None:
    _page(ws, A, name="5e", aliases=["5e", "D&D"])
    res = Resolution(kind="disambiguate", renames={A: RenameSpec(slug="dnd-5e")},
                     alias_moves=[AliasMove("5e", B)])
    apply_resolution(ws, res, A, B, actor="user")
    assert "5e" not in [a.lower() for a in _load(ws, "topic:dnd-5e").aliases]
    assert "5e" in _load(ws, B).aliases


def test_keep_on_both_copies_the_alias(ws: Path) -> None:
    apply_resolution(ws, Resolution(kind="disambiguate",
                                    alias_moves=[AliasMove("D&D 5e", "both")]), A, B,
                     actor="user")
    assert "D&D 5e" in _load(ws, B).aliases and "D&D 5e" in _load(ws, A).aliases


def test_dream_never_merges_a_tombstoned_pair(ws: Path) -> None:
    from durin.memory.refine_dream import add_tombstone
    add_tombstone(ws, A, B)
    with pytest.raises(ResolutionError):
        apply_resolution(ws, Resolution(kind="merge", survivor=B), A, B, actor="dream")
    assert (ws / "memory/entities/topic/5e.md").exists()


def test_stale_merge_is_reported_not_silently_ok(ws: Path) -> None:
    from durin.memory.pair_resolution import PairPageMissingError
    apply_resolution(ws, Resolution(kind="merge", survivor=B), A, B, actor="user")
    with pytest.raises(PairPageMissingError):
        apply_resolution(ws, Resolution(kind="merge", survivor=B), A, B, actor="user")


def test_a_concurrent_edit_is_not_overwritten(ws: Path, monkeypatch) -> None:
    """A write that lands between reading the pages and committing the
    resolution is detected; the resolution is recomputed on top of it."""
    import durin.memory.memory_writer as mw
    from durin.memory.memory_writer import write_files_cas

    calls = {"n": 0}
    real = mw.write_files_cas

    def racing(workspace, changes, **kw):
        if calls["n"] == 0:
            calls["n"] += 1
            page = _load(ws, B)
            page.body = "edited concurrently"
            write_files_cas(ws, {"entities/topic/dungeons-and-dragons.md":
                                 page.to_markdown().encode("utf-8")}, message="concurrent")
        return real(workspace, changes, **kw)

    monkeypatch.setattr(mw, "write_files_cas", racing)
    apply_resolution(ws, Resolution(kind="disambiguate",
                                    alias_moves=[AliasMove("D&D", B)]), A, B, actor="user")
    game = _load(ws, B)
    assert calls["n"] == 1
    assert game.body.strip() == "edited concurrently"
    assert "D&D" in game.aliases and "D&D" not in _load(ws, A).aliases
