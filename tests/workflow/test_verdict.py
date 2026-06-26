"""Tests for parse_verdict and parse_label: the verdict contracts for routing agent nodes."""

from durin.workflow.verdict import parse_label, parse_verdict


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


# --- parse_label tests ---


def test_parse_label_matches_last_line():
    labels = ["GROUNDED", "MISSING", "MISUSED"]
    text = "Some analysis here.\nMISSING\nGROUNDED"
    # Last matching line is GROUNDED
    assert parse_label(text, labels) == "GROUNDED"


def test_parse_label_prefers_last_over_earlier():
    labels = ["DONE", "RETRY"]
    text = "DONE\nsome stuff\nRETRY"
    assert parse_label(text, labels) == "RETRY"


def test_parse_label_case_insensitive():
    assert parse_label("grounded", ["GROUNDED", "MISSING"]) == "GROUNDED"
    assert parse_label("Missing", ["GROUNDED", "MISSING"]) == "MISSING"


def test_parse_label_strips_surrounding_punctuation():
    labels = ["GROUNDED", "MISSING"]
    assert parse_label("GROUNDED.", labels) == "GROUNDED"
    assert parse_label("**MISSING**", labels) == "MISSING"
    assert parse_label("  GROUNDED!  ", labels) == "GROUNDED"


def test_parse_label_no_substring_false_match():
    # "GROUNDED" must not match a line "GROUNDED_EXTRA" or "UNGROUNDED"
    labels = ["GROUNDED"]
    assert parse_label("GROUNDED_EXTRA", labels) is None
    assert parse_label("UNGROUNDED", labels) is None


def test_parse_label_returns_none_on_no_match():
    assert parse_label("some random output", ["GROUNDED", "MISSING"]) is None


def test_parse_label_empty_text():
    assert parse_label("", ["GROUNDED"]) is None


def test_parse_label_skips_empty_lines():
    text = "\n\nGROUNDED\n\n"
    assert parse_label(text, ["GROUNDED", "MISSING"]) == "GROUNDED"


def test_parse_label_preserves_original_label_case():
    # The label "Grounded" (mixed case) should be returned as-is when matched.
    assert parse_label("GROUNDED", ["Grounded", "Missing"]) == "Grounded"
