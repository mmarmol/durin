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


# The node's instruction asks for the verdict on the LAST line. A live judge
# answer opened with a summary sentence, graded each criterion on its own line,
# and ended with the verdict: read from the first line it was a FAIL.
_JUDGE_ANSWER = (
    "All spot-checks verified — the second named instance matches the note exactly.\n\n"
    "## Audit complete\n\n"
    "**1. CORRECTNESS — PASS.** Every technical claim cross-checked.\n\n"
    "**2. EVIDENCE SUFFICIENCY — PASS.** All modules cited.\n\n"
    "PASS"
)


def test_the_verdict_is_read_from_the_last_line():
    assert parse_verdict(_JUDGE_ANSWER) is True
    assert parse_verdict("The note misstates the expiry.\n\nFAIL — fix the expiry date") is False
    assert parse_verdict("Looks right.\n\n**PASS**") is True


def test_a_later_verdict_line_overrides_an_earlier_one():
    assert parse_verdict("PASS\nOn a second look the totals are wrong.\nFAIL: recompute them") is False
    assert parse_verdict("FAIL\nRe-checked after the retry; it holds.\nPASS.") is True


def test_a_verdict_on_the_first_line_still_counts():
    """Prompts written for the old first-line reading keep routing the same way,
    including a first line that goes on in words after the verdict."""
    assert parse_verdict("PASS\nAll criteria met.") is True
    assert parse_verdict("PASS second pass") is True
    assert parse_verdict("FAIL first pass") is False
    assert parse_verdict("FAIL\n- add the missing test\n- rename the helper") is False


def test_prose_that_starts_with_the_word_never_passes_a_failed_check():
    """Read from the end, a line that goes on in words after PASS is prose — a
    bullet of a FAIL's fix list — so it can never flip the verdict to PASS. FAIL
    lines may go on: 'FAIL' followed by what to fix is the verdict's own form."""
    assert parse_verdict("FAIL\n- Pass the ticket id to the handler.") is False
    assert parse_verdict("PASS\nOn a second look it is wrong.\nFAIL the totals are off") is False
    assert parse_verdict("Checked.\nPASS — with the caveats noted above") is True
    assert parse_verdict("Review done.\nPASS (minor wording nits only)") is True
    assert parse_verdict("Fail-safe defaults are in place.") is False


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


# --- strip_verdict_line tests ---


def test_strip_verdict_line_removes_leading_pass():
    from durin.workflow.verdict import strip_verdict_line
    assert strip_verdict_line("PASS\nAll good, ship it.") == "All good, ship it."


def test_strip_verdict_line_keeps_text_without_verdict():
    from durin.workflow.verdict import strip_verdict_line
    assert strip_verdict_line("just prose") == "just prose"


def test_strip_verdict_line_empty_when_only_verdict():
    from durin.workflow.verdict import strip_verdict_line
    assert strip_verdict_line("PASS") == ""


def test_strip_verdict_line_removes_the_line_the_verdict_was_read_from():
    from durin.workflow.verdict import strip_verdict_line
    assert strip_verdict_line("Checked all three claims.\n\nPASS") == "Checked all three claims."
    assert strip_verdict_line(_JUDGE_ANSWER) == _JUDGE_ANSWER.rsplit("\n\nPASS", 1)[0]
    # A criterion line that only mentions a verdict stays.
    assert "CORRECTNESS — PASS" in strip_verdict_line(_JUDGE_ANSWER)


# --- strip_label_line tests ---


def test_strip_label_line_removes_trailing_label():
    from durin.workflow.verdict import strip_label_line
    assert strip_label_line("The claims are grounded.\n\nGROUNDED", ["GROUNDED", "MISSING"]) \
        == "The claims are grounded."


def test_strip_label_line_tolerates_punctuation():
    from durin.workflow.verdict import strip_label_line
    assert strip_label_line("done\n**GROUNDED**", ["GROUNDED"]) == "done"
