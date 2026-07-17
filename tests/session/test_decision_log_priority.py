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
