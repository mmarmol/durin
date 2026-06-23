"""Tests for parse_verdict: the pass/fail contract for routing agent nodes."""

from durin.workflow.verdict import parse_verdict


def test_pass_first_line():
    assert parse_verdict("PASS\nlooks good") is True


def test_fail_default():
    assert parse_verdict("FAIL — missing tests") is False


def test_unrecognised_defaults_to_fail():
    assert parse_verdict("hmm, not sure") is False


def test_case_and_whitespace_insensitive():
    assert parse_verdict("  pass  ") is True


def test_empty_defaults_to_fail():
    assert parse_verdict("") is False


def test_none_defaults_to_fail():
    assert parse_verdict(None) is False
