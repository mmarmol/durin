"""Manual note_decision entries outrank auto-extracted ones at eviction.

A 10-entry auto burst at compaction time must not flush the operator's
manual notes (2026-07-17 incident: the Zendesk note held the answer).
"""
from __future__ import annotations

from durin.session.decision_log import add_decision, parse_decisions


def _fill(metadata: dict, n: int, source: str, prefix: str) -> None:
    for i in range(n):
        add_decision(
            metadata, f"{prefix} {i}", source=source, max_entries=10,
            max_chars=1500,
        )


def test_auto_burst_does_not_evict_manual_entries() -> None:
    metadata: dict = {}
    add_decision(metadata, "manual: zendesk skill at skills/zendesk", source="tool",
                 max_entries=10, max_chars=1500)
    _fill(metadata, 12, "auto", "auto fact")
    entries = parse_decisions(metadata["decision_log"])
    texts = [e["text"] for e in entries]
    assert any(t.startswith("manual:") for t in texts)
    assert len(entries) == 10


def test_oldest_auto_evicted_first() -> None:
    metadata: dict = {}
    _fill(metadata, 10, "auto", "auto fact")
    add_decision(metadata, "one more", source="auto", max_entries=10, max_chars=1500)
    texts = [e["text"] for e in parse_decisions(metadata["decision_log"])]
    assert "auto fact 0" not in texts
    assert "one more" in texts


def test_all_manual_still_bounded() -> None:
    metadata: dict = {}
    _fill(metadata, 12, "tool", "manual note")
    entries = parse_decisions(metadata["decision_log"])
    assert len(entries) == 10
    assert entries[-1]["text"] == "manual note 11"


# ===========================================================================
# An auto append must never be a silent no-op, and must never cost the
# operator a manual anchor. Both were live on a saturated log: five manual
# entries filling 1,411 of 1,500 chars left 89 chars of headroom, so every
# auto bullet the consolidator extracted (70-340 chars observed) either
# evicted itself or evicted an anchor.
# ===========================================================================


def _saturated(n_manual: int = 5, chars_each: int = 280) -> dict:
    return {
        "decision_log": [
            {"text": f"m{i}" + "x" * (chars_each - 2), "ts": "", "source": "tool"}
            for i in range(n_manual)
        ]
    }


def test_auto_append_never_evicts_itself():
    """The entry just written is never the one dropped to satisfy the caps."""
    metadata = _saturated()
    cap = sum(len(e["text"]) for e in metadata["decision_log"]) + 200

    entries, _ = add_decision(
        metadata, "a decision worth keeping", source="auto", max_entries=10, max_chars=cap,
    )
    assert entries[-1]["text"] == "a decision worth keeping"
    assert entries[-1]["source"] == "auto"


def test_auto_append_is_rejected_rather_than_evicting_a_manual_anchor():
    """When the only way to fit is dropping an operator anchor, back out."""
    metadata = _saturated()
    before = [dict(e) for e in metadata["decision_log"]]
    cap = sum(len(e["text"]) for e in before)  # zero headroom

    entries, dropped = add_decision(
        metadata, "x" * 300, source="auto", max_entries=10, max_chars=cap,
    )
    assert entries == before, "manual anchors must be untouched"
    assert not any(e["source"] == "auto" for e in entries)
    assert dropped == 1, "the loss is still reported so telemetry sees it"


def test_manual_append_still_evicts_by_documented_priority():
    """Manual entries outrank auto ones: an auto is dropped first, and a manual
    append may still evict the oldest manual once no auto remains."""
    metadata = _saturated(n_manual=2)
    metadata["decision_log"].insert(
        1, {"text": "auto entry" + "y" * 270, "ts": "", "source": "auto"},
    )
    cap = sum(len(e["text"]) for e in metadata["decision_log"])

    entries, dropped = add_decision(
        metadata, "z" * 280, source="tool", max_entries=10, max_chars=cap,
    )
    assert dropped >= 1
    assert not any(e["source"] == "auto" for e in entries), "auto evicted first"
    assert entries[-1]["text"] == "z" * 280, "the new manual entry survives"


def test_auto_entries_accumulate_when_there_is_headroom():
    """With the caps giving the auto channel room, consecutive compactions
    accumulate instead of overwriting each other."""
    metadata = _saturated(n_manual=2, chars_each=100)
    for i in range(4):
        add_decision(
            metadata, f"auto finding {i}", source="auto", max_entries=10, max_chars=3000,
        )
    autos = [e for e in metadata["decision_log"] if e["source"] == "auto"]
    assert len(autos) == 4
    assert [e["text"] for e in autos] == [f"auto finding {i}" for i in range(4)]
    assert len([e for e in metadata["decision_log"] if e["source"] == "tool"]) == 2
