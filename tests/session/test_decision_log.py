"""Tests for the decision_log task-state subsystem."""
from __future__ import annotations

from durin.session.decision_log import (
    DECISION_LOG_KEY,
    add_decision,
    decision_log_runtime_lines,
    parse_decisions,
)


def test_add_decision_appends_and_returns_no_drop():
    meta: dict = {}
    entries, dropped = add_decision(meta, "chose separate call", source="tool", ts="t1")
    assert dropped == 0
    assert len(entries) == 1
    assert meta[DECISION_LOG_KEY][0]["text"] == "chose separate call"
    assert meta[DECISION_LOG_KEY][0]["source"] == "tool"


def test_add_decision_dedups_normalized_text():
    meta: dict = {}
    add_decision(meta, "Spill marker keeps the path", source="tool", ts="t1")
    entries, dropped = add_decision(meta, "  spill   marker KEEPS the path  ", source="auto", ts="t2")
    assert len(entries) == 1  # normalized duplicate skipped
    assert dropped == 0


def test_add_decision_caps_by_entry_count_and_reports_dropped():
    meta: dict = {}
    last_dropped = 0
    for i in range(15):
        _, last_dropped = add_decision(meta, f"decision number {i}", source="auto", ts="t", max_entries=10)
    entries = meta[DECISION_LOG_KEY]
    assert len(entries) == 10
    assert entries[-1]["text"] == "decision number 14"  # newest kept
    assert entries[0]["text"] == "decision number 5"   # oldest dropped
    assert last_dropped == 1  # the 15th add dropped exactly one


def test_add_decision_caps_by_total_chars():
    meta: dict = {}
    big = "x" * 300
    for i in range(10):
        add_decision(meta, f"{big}{i}", source="auto", ts="t", max_chars=1000)
    total = sum(len(e["text"]) for e in meta[DECISION_LOG_KEY])
    assert total <= 1000


def test_add_decision_ignores_blank():
    meta: dict = {}
    entries, dropped = add_decision(meta, "   ", source="tool", ts="t1")
    assert entries == []
    assert dropped == 0
    assert meta.get(DECISION_LOG_KEY) in (None, [])


def test_runtime_lines_empty_when_no_decisions():
    assert decision_log_runtime_lines({}) == []
    assert decision_log_runtime_lines(None) == []


def test_runtime_lines_render_bullets_newest_last():
    meta: dict = {}
    add_decision(meta, "first decision", source="tool", ts="t1")
    add_decision(meta, "second decision", source="auto", ts="t2")
    assert decision_log_runtime_lines(meta) == ["  - first decision", "  - second decision"]


def test_parse_decisions_drops_malformed():
    out = parse_decisions([{"text": "ok"}, {"nope": 1}, "junk", {"text": ""}])
    assert [e["text"] for e in out] == ["ok"]
