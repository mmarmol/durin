"""run_rereview: the Bandeja and the kept-separate pairs, re-judged."""

from __future__ import annotations

from pathlib import Path

from durin.memory.absorb_judge import JudgeResult
from durin.memory.entity_page import EntityPage
from durin.memory.pair_rereview import run_rereview
from durin.memory.refine_dream import (
    add_flagged,
    add_tombstone,
    is_tombstoned,
    read_flagged,
    read_tombstones,
)

A, B, C, D = "topic:5e", "topic:dnd", "person:ana", "person:ana_b"
RELATE = {"renames": {A: {"slug": "dnd-5e"}},
          "alias_moves": [{"alias": "D&D", "keep_on": B}],
          "relation": {"from": A, "type": "edition_of", "to": B}}


def _page(ws: Path, ref: str, name: str, aliases: list[str]) -> None:
    t, _, s = ref.partition(":")
    EntityPage(type=t, name=name, aliases=aliases, body=".", author="agent_created").save(
        ws / "memory" / "entities" / t / f"{s}.md")


def _exists(ws: Path, ref: str) -> bool:
    t, _, s = ref.partition(":")
    return (ws / "memory" / "entities" / t / f"{s}.md").exists()


def _judge(answers: dict):
    calls = []

    def judge(ws, a, b, *, user_kept_separate):
        calls.append((a, b, user_kept_separate))
        return answers[(a, b)]
    judge.calls = calls
    return judge


def _ws(tmp_path: Path) -> Path:
    _page(tmp_path, A, "5e", ["D&D"])
    _page(tmp_path, B, "Dungeons & Dragons", ["D&D"])
    _page(tmp_path, C, "Ana", ["Ana"])
    _page(tmp_path, D, "Ana B", ["Ana"])
    return tmp_path


def test_pending_confident_resolution_is_applied(tmp_path):
    ws = _ws(tmp_path)
    add_flagged(ws, A, B, verdict="different", confidence=70, reasoning="old")
    judge = _judge({(A, B): JudgeResult("related", 92, "edition", proposal=RELATE)})
    out = run_rereview(ws, separated=False, judge=judge)
    [row] = out["pairs"]
    assert (row["outcome"], row["now"]) == ("resolved", ["topic:dnd-5e", B])
    assert read_flagged(ws) == []
    assert judge.calls == [(A, B, False)]


def test_pending_unsure_is_reflagged_with_the_proposal(tmp_path):
    ws = _ws(tmp_path)
    add_flagged(ws, A, B, verdict="different", confidence=70, reasoning="old")
    run_rereview(ws, separated=False, judge=_judge(
        {(A, B): JudgeResult("related", 70, "maybe", proposal=RELATE)}))
    [rec] = read_flagged(ws)
    assert (rec["source"], rec["proposal"]["kind"]) == ("rereview", "relate")
    assert _exists(ws, A)


def test_pending_confident_different_is_settled_and_leaves_the_bandeja(tmp_path):
    ws = _ws(tmp_path)
    add_flagged(ws, C, D, verdict="unclear", confidence=60, reasoning="old")
    out = run_rereview(ws, separated=False, judge=_judge(
        {(C, D): JudgeResult("different", 90, "two people")}))
    assert out["pairs"][0]["outcome"] == "settled"
    assert read_flagged(ws) == []


def test_pending_confident_same_merges(tmp_path):
    ws = _ws(tmp_path)
    add_flagged(ws, C, D, verdict="unclear", confidence=60, reasoning="old")
    out = run_rereview(ws, separated=False, merge_threshold=80, judge=_judge(
        {(C, D): JudgeResult("same", 90, "same person", proposal={"survivor": D})}))
    assert out["pairs"][0]["outcome"] == "merged"
    assert _exists(ws, D) and not _exists(ws, C)


def test_stale_pending_pair_is_dropped(tmp_path):
    ws = _ws(tmp_path)
    add_flagged(ws, A, "topic:gone", verdict="unclear", confidence=60, reasoning="old")
    judge = _judge({})
    out = run_rereview(ws, separated=False, judge=judge)
    assert out["pairs"][0]["outcome"] == "stale"
    assert read_flagged(ws) == [] and judge.calls == []


def test_separated_pairs_are_never_merged_and_proposals_go_to_the_bandeja(tmp_path):
    ws = _ws(tmp_path)
    add_tombstone(ws, A, B)
    add_tombstone(ws, C, D)
    judge = _judge({
        (A, B): JudgeResult("related", 95, "edition", proposal=RELATE),
        (C, D): JudgeResult("same", 99, "looks same", proposal={"survivor": C}),
    })
    out = run_rereview(ws, pending=False, judge=judge)
    outcomes = {tuple(r["pair"]): r["outcome"] for r in out["pairs"]}
    assert outcomes == {(A, B): "flagged", (C, D): "kept"}
    assert all(kept for *_, kept in judge.calls)  # told the user kept them apart
    assert _exists(ws, C) and _exists(ws, D)
    [rec] = read_flagged(ws)
    assert rec["pair"] == sorted([A, B]) and rec["proposal"]["kind"] == "relate"


def test_apply_separated_applies_and_keeps_the_tombstone(tmp_path):
    ws = _ws(tmp_path)
    add_tombstone(ws, A, B)
    run_rereview(ws, pending=False, apply_separated=True, judge=_judge(
        {(A, B): JudgeResult("related", 95, "edition", proposal=RELATE)}))
    assert _exists(ws, "topic:dnd-5e")
    assert is_tombstoned(ws, "topic:dnd-5e", B)
    assert read_flagged(ws) == []


def test_dry_run_writes_nothing(tmp_path):
    ws = _ws(tmp_path)
    add_flagged(ws, A, B, verdict="different", confidence=70, reasoning="old")
    add_tombstone(ws, C, D)
    out = run_rereview(ws, dry_run=True, judge=_judge({
        (A, B): JudgeResult("related", 95, "edition", proposal=RELATE),
        (C, D): JudgeResult("different", 60, "?", proposal={
            "alias_moves": [{"alias": "Ana", "keep_on": C}]}),
    }))
    assert [r["outcome"] for r in out["pairs"]] == ["would_resolve", "would_flag"]
    assert _exists(ws, A)
    assert len(read_flagged(ws)) == 1 and read_flagged(ws)[0]["reasoning"] == "old"
    assert read_tombstones(ws) == [(C, D)]


def test_limit_stops_the_pass(tmp_path):
    ws = _ws(tmp_path)
    add_tombstone(ws, A, B)
    add_tombstone(ws, C, D)
    out = run_rereview(ws, pending=False, limit=1, judge=_judge({
        (A, B): JudgeResult("different", 90, "x"),
        (C, D): JudgeResult("different", 90, "x")}))
    assert len(out["pairs"]) == 1 and out["stopped"] == "limit"


def test_unclear_merge_proposal_never_merges_a_kept_separate_pair(tmp_path):
    ws = _ws(tmp_path)
    add_tombstone(ws, C, D)
    out = run_rereview(ws, pending=False, judge=_judge(
        {(C, D): JudgeResult("unclear", 95, "?", proposal={"action": "merge", "survivor": C})}))
    assert out["pairs"][0]["outcome"] == "kept"
    assert _exists(ws, C) and _exists(ws, D)
    assert is_tombstoned(ws, C, D)


def test_its_own_proposal_for_a_kept_separate_pair_survives_the_next_run(tmp_path):
    ws = _ws(tmp_path)
    add_tombstone(ws, A, B)
    judge = _judge({(A, B): JudgeResult("related", 95, "edition", proposal=RELATE)})
    run_rereview(ws, pending=False, judge=judge)
    assert len(read_flagged(ws)) == 1
    run_rereview(ws, separated=False, judge=judge)  # pending-only run
    assert len(read_flagged(ws)) == 1


def test_auto_resolve_off_flags_instead_of_applying(tmp_path):
    ws = _ws(tmp_path)
    add_flagged(ws, A, B, verdict="different", confidence=70, reasoning="old")
    out = run_rereview(ws, separated=False, auto_resolve=False, judge=_judge(
        {(A, B): JudgeResult("related", 95, "edition", proposal=RELATE)}))
    assert out["pairs"][0]["outcome"] == "flagged"
    assert _exists(ws, A)


def test_a_flagged_pair_the_user_kept_apart_is_reviewed_as_separated(tmp_path):
    ws = _ws(tmp_path)
    add_flagged(ws, A, B, verdict="unclear", confidence=60, reasoning="old")
    add_tombstone(ws, A, B)
    judge = _judge({(A, B): JudgeResult("related", 95, "edition", proposal=RELATE)})
    out = run_rereview(ws, judge=judge)
    assert [(r["group"], r["outcome"]) for r in out["pairs"]] == [("separated", "flagged")]
    assert judge.calls == [(A, B, True)]
    [rec] = read_flagged(ws)
    assert rec["source"] == "rereview"
