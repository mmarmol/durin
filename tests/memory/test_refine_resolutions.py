"""run_refine applying the judges' resolutions on its own."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import durin.memory.refine_dream as rd
from durin.memory.absorb_judge import JudgeResult
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.refine_dream import read_flagged, read_tombstones, run_refine

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
A, B = "topic:5e", "topic:dungeons_and_dragons"


def _stub(verdict: str, conf: int, proposal: dict | None = None, counter=None):
    def inv(prompt, **kw):
        if counter is not None:
            counter["n"] += 1
        block = f"===RESOLUTION===\n{json.dumps(proposal)}\n" if proposal is not None else ""
        return (f"===VERDICT===\n{verdict}\n===CONFIDENCE===\n{conf}\n"
                f"===REASONING===\nstub\n{block}===END===")
    return inv


def _pair(ws) -> None:
    for ref, name, aliases in ((A, "5e", ["D&D", "juego D&D original"]),
                               (B, "Dungeons & Dragons", ["D&D"])):
        write_entity(ws, ref, [FieldPatch(kind="alias", value=a, author="agent",
                                          source_ref="s", at=NOW) for a in aliases],
                     create=True, name=name)


def _load(ws, ref):
    t, _, s = ref.partition(":")
    return EntityPage.from_file(ws / "memory" / "entities" / t / f"{s}.md")


RELATE = {"renames": {A: {"slug": "dnd_5e"}},
          "alias_moves": [{"alias": "juego D&D original", "keep_on": B}],
          "relation": {"from": A, "type": "edition_of", "to": B}}


def test_confident_related_is_applied_by_the_dream(tmp_path):
    _pair(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_stub("related", 90, RELATE),
                     resolve_threshold=85, escalate_floor=0)
    assert out["resolved"] and out["resolved"][0]["kind"] == "relate"
    assert out["resolved"][0]["now"] == ["topic:dnd_5e", B]
    edition = _load(tmp_path, "topic:dnd_5e")
    assert edition.relations == [{"to": B, "type": "edition_of"}]
    assert "juego D&D original" in _load(tmp_path, B).aliases
    assert read_flagged(tmp_path) == []
    assert read_tombstones(tmp_path) == []  # the dream never tombstones


def test_resolved_pair_is_not_rejudged(tmp_path):
    _pair(tmp_path)
    run_refine(tmp_path, llm_invoke=_stub("related", 90, RELATE), escalate_floor=0)
    counter = {"n": 0}
    out = run_refine(tmp_path, llm_invoke=_stub("related", 90, RELATE, counter),
                     escalate_floor=0)
    assert counter["n"] == 0, out  # still share "D&D", but the verdict is remembered


def test_auto_resolve_off_flags_the_proposal(tmp_path):
    _pair(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_stub("related", 90, RELATE),
                     auto_resolve=False, escalate_floor=0)
    assert out["resolved"] == []
    [rec] = read_flagged(tmp_path)
    assert rec["proposal"]["kind"] == "relate"
    assert rec["source"] == "tier1"
    assert _load(tmp_path, A) is not None  # untouched


def test_auto_rename_off_sends_a_renaming_proposal_to_the_bandeja(tmp_path):
    _pair(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_stub("related", 90, RELATE),
                     auto_rename=False, escalate_floor=0)
    assert out["resolved"] == []
    [rec] = read_flagged(tmp_path)
    assert rec["proposal"]["renames"] == {A: {"slug": "dnd_5e", "name": None}}
    assert _load(tmp_path, A).relations == []


def test_auto_rename_off_still_applies_a_proposal_without_renames(tmp_path):
    _pair(tmp_path)
    no_rename = {k: v for k, v in RELATE.items() if k != "renames"}
    run_refine(tmp_path, llm_invoke=_stub("related", 90, no_rename),
               auto_rename=False, escalate_floor=0)
    assert _load(tmp_path, A).relations == [{"to": B, "type": "edition_of"}]


def test_unclear_never_changes_memory_on_its_own(tmp_path):
    _pair(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_stub("unclear", 95, {"kind": "merge", "survivor": B}),
                     escalate_floor=0)
    assert out["merged"] == [] and out["resolved"] == []
    assert _load(tmp_path, A) is not None and _load(tmp_path, B) is not None


def test_noop_proposal_is_settled_not_reported(tmp_path):
    _pair(tmp_path)
    # "D&D" is already on both pages: keeping it on both changes nothing.
    out = run_refine(tmp_path, llm_invoke=_stub("different", 90, {
        "alias_moves": [{"alias": "D&D", "keep_on": "both"}]}), escalate_floor=0)
    assert out["resolved"] == []
    assert read_flagged(tmp_path) == []


def test_malformed_proposal_does_not_end_the_pass(tmp_path):
    _pair(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_stub("different", 90, {
        "survivor": ["x"], "alias_moves": 3, "renames": {A: {"slug": {"a": 1}}},
        "relation": {"from": [], "type": 7, "to": None}}), escalate_floor=0)
    assert out["judged"] == 1 and out["resolved"] == []


def test_the_investigating_judges_proposal_is_applied_whatever_its_confidence(
        tmp_path, monkeypatch):
    """The investigating judge read both pages and their lineage. A change it
    proposes short of a merge removes nothing and can be reverted, and people
    applied such proposals down to 55% by hand — so it is applied, not queued."""
    _pair(tmp_path)
    monkeypatch.setattr(rd, "_escalate_judge", lambda ws, a, b, **kw: JudgeResult(
        "related", 62, "probably an edition", proposal=RELATE))
    out = run_refine(tmp_path, llm_invoke=_stub("related", 75, RELATE),
                     escalate_floor=70, resolve_threshold=85)
    assert out["resolved"] and out["resolved"][0]["kind"] == "relate"
    assert read_flagged(tmp_path) == []
    assert _load(tmp_path, "topic:dnd_5e").relations == [{"to": B, "type": "edition_of"}]


def test_an_unclear_investigating_judge_still_asks_a_person(tmp_path, monkeypatch):
    _pair(tmp_path)
    monkeypatch.setattr(rd, "_escalate_judge", lambda ws, a, b, **kw: JudgeResult(
        "unclear", 80, "cannot tell", proposal=RELATE))
    out = run_refine(tmp_path, llm_invoke=_stub("unclear", 80),
                     escalate_floor=70, resolve_threshold=85)
    assert out["resolved"] == [] and out["merged"] == []
    [rec] = read_flagged(tmp_path)
    assert (rec["source"], rec["verdict"]) == ("tier2", "unclear")


def test_the_investigating_judges_merge_keeps_its_floor(tmp_path, monkeypatch):
    """A merge folds two pages into one, so it keeps a confidence floor: at or
    above it the investigating judge merges, below it a person decides."""
    merge = {"kind": "merge", "survivor": B}
    _pair(tmp_path)
    monkeypatch.setattr(rd, "_escalate_judge", lambda ws, a, b, **kw: JudgeResult(
        "same", 59, "one game", proposal=merge))
    out = run_refine(tmp_path, llm_invoke=_stub("same", 80, merge),
                     escalate_floor=70, tier2_confidence_threshold=60)
    assert out["merged"] == []
    assert [r["verdict"] for r in read_flagged(tmp_path)] == ["same"]

    monkeypatch.setattr(rd, "_escalate_judge", lambda ws, a, b, **kw: JudgeResult(
        "same", 60, "one game", proposal=merge))
    out = run_refine(tmp_path, llm_invoke=_stub("same", 80, merge),
                     escalate_floor=70, tier2_confidence_threshold=60, recheck_cooldown_s=0)
    assert [m["canonical"] for m in out["merged"]] == [B]
    assert not (tmp_path / "memory" / "entities" / "topic" / "5e.md").exists()


def test_the_investigating_judges_merge_floor_defaults_to_60():
    from durin.config.schema import AutoAbsorbConfig
    assert AutoAbsorbConfig().tier2_confidence_threshold == 60


def test_tier2_can_apply_its_own_confident_proposal(tmp_path, monkeypatch):
    _pair(tmp_path)
    monkeypatch.setattr(rd, "_escalate_judge", lambda ws, a, b, **kw: JudgeResult(
        "related", 92, "edition of the game", proposal=RELATE))
    out = run_refine(tmp_path, llm_invoke=_stub("unclear", 80),
                     escalate_floor=70, resolve_threshold=85)
    assert out["resolved"], out
    assert read_flagged(tmp_path) == []


def test_confident_tier2_different_is_not_flagged(tmp_path, monkeypatch):
    _pair(tmp_path)
    monkeypatch.setattr(rd, "_escalate_judge", lambda ws, a, b, **kw: JudgeResult(
        "different", 90, "distinct"))
    run_refine(tmp_path, llm_invoke=_stub("unclear", 80), escalate_floor=70)
    assert read_flagged(tmp_path) == []


def test_the_investigating_judges_different_settles_the_pair(tmp_path, monkeypatch):
    """Two pages the investigating judge found distinct, with nothing to
    change, are not a question for a person, however sure it was."""
    _pair(tmp_path)
    monkeypatch.setattr(rd, "_escalate_judge", lambda ws, a, b, **kw: JudgeResult(
        "different", 55, "not sure"))
    run_refine(tmp_path, llm_invoke=_stub("unclear", 80), escalate_floor=70)
    assert read_flagged(tmp_path) == []


def test_merge_goes_into_the_judges_survivor(tmp_path):
    _pair(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_stub("same", 97, {
        "survivor": B, "renames": {B: {"slug": "dnd"}}}), escalate_floor=0)
    assert out["merged"] == [{"canonical": "topic:dnd", "absorbed": A, "confidence": 97}]
    assert _load(tmp_path, "topic:dnd") is not None
    assert not (tmp_path / "memory/entities/topic/5e.md").exists()


def test_unusable_merge_proposal_falls_back_to_ref_a(tmp_path):
    _pair(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_stub("same", 97, {"survivor": "topic:zzz"}),
                     escalate_floor=0)
    assert out["merged"][0]["canonical"] == A


def test_merge_respects_a_tombstone_written_during_the_run(tmp_path, monkeypatch):
    """The run's tombstone snapshot is older than a decision the user (or an
    earlier merge in the run) just wrote: the merge must still be refused."""
    _pair(tmp_path)
    real_load = rd._load_tombstones
    calls = {"n": 0}

    def stale_snapshot(ws):
        calls["n"] += 1
        return set() if calls["n"] == 1 else real_load(ws)  # first load = the snapshot

    monkeypatch.setattr(rd, "_load_tombstones", stale_snapshot)
    rd.add_tombstone(tmp_path, A, B)
    out = run_refine(tmp_path, llm_invoke=_stub("same", 99, {"survivor": B}), escalate_floor=0)
    assert out["merged"] == []
    assert _load(tmp_path, A) is not None and _load(tmp_path, B) is not None
    assert any(s["reason"] == "tombstoned" for s in out["skipped"])


def test_a_failing_merge_does_not_end_the_pass(tmp_path, monkeypatch):
    from durin.memory.absorption import AbsorptionError, EntityAbsorption

    _pair(tmp_path)

    def boom(self, *a, **kw):
        raise AbsorptionError("disk on fire")

    monkeypatch.setattr(EntityAbsorption, "absorb", boom)
    out = run_refine(tmp_path, llm_invoke=_stub("same", 99), escalate_floor=0)
    assert out["merged"] == []
    assert any(s["reason"].startswith("merge_failed") for s in out["skipped"])


def test_judge_both_does_not_copy_an_alias_across(tmp_path):
    _pair(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_stub("different", 95, {
        "alias_moves": [{"alias": "juego D&D original", "keep_on": "both"}]}), escalate_floor=0)
    assert out["resolved"] == []
    assert "juego D&D original" not in _load(tmp_path, B).aliases
