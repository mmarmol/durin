"""durin.automations.judge — the trigger-filter judge's prompt/verdict pair."""

from __future__ import annotations

from durin.automations.judge import build_filter_prompt, parse_filter_verdict


def test_parse_filter_verdict_happy():
    assert parse_filter_verdict('noise {"match": true} noise') is True
    assert parse_filter_verdict('{"match": false}') is False


def test_parse_filter_verdict_garbage_defaults_to_false():
    assert parse_filter_verdict("no json here") is False
    assert parse_filter_verdict("") is False


def test_build_filter_prompt_includes_condition_and_summary():
    p = build_filter_prompt("is urgent", "From: a@b.com\nSubject: help")
    assert "is urgent" in p
    assert "From: a@b.com" in p
