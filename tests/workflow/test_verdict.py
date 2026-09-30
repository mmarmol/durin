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


def test_a_verdict_at_the_end_is_read():
    assert parse_verdict(_JUDGE_ANSWER) is True
    assert parse_verdict("The note misstates the expiry.\n\nFAIL — fix the expiry date") is False
    assert parse_verdict("Looks right.\n\n**PASS**") is True
    assert parse_verdict("All good.\nPASS\n\n-- judge") is True
    assert parse_verdict("Done.\n**PASS** all criteria met") is True


def test_labelled_verdicts_are_read():
    assert parse_verdict("## Review\n\n**Verdict:** FAIL") is False
    assert parse_verdict("Checked.\n\n**Verdict: PASS**") is True
    assert parse_verdict("Overall: FAIL — fix the expiry date.") is False
    assert parse_verdict("Final verdict: PASS.") is True


def test_the_caveat_forms_of_a_pass_are_read():
    assert parse_verdict("Checked.\nPASS — with the caveats noted above") is True
    assert parse_verdict("Review done.\nPASS (minor wording nits only)") is True
    assert parse_verdict("The note is acceptable.\n\nPASS with noted caveats: the Slack link is missing") is True


def test_competing_verdict_lines_read_as_fail():
    """Every line that states a verdict must agree: a PASS beside any FAIL is a FAIL,
    whichever comes last, so disagreement can only err toward another pass."""
    assert parse_verdict("PASS\nOn a second look the totals are wrong.\nFAIL: recompute them") is False
    assert parse_verdict("FAIL\nRe-checked after the retry; it holds.\nPASS.") is False
    assert parse_verdict("Checks:\nPASS: tone\n\nThe totals are wrong.\nFAILED") is False
    # A quoted test-output line counts too: the cost of never passing on disagreement.
    assert parse_verdict("PASS\n\nThe earlier run printed:\nFAIL tests/test_x.py::test_a") is False


def test_checklists_code_and_quotes_are_not_verdicts():
    checklist = ("## Review\n\n- FAIL: the expiry date is wrong\n- PASS: tone\n\n**Verdict:** FAIL")
    assert parse_verdict(checklist) is False
    emoji = "✅ PASS: citations resolve\n✅ PASS: tone\n❌ FAIL: the expiry date\n\n**Verdict: FAIL**"
    assert parse_verdict(emoji) is False
    what_is_fine = "FAIL — the expiry date is wrong.\n\nWhat is fine:\n- PASS: tone and structure"
    assert parse_verdict(what_is_fine) is False
    stub = "FAIL - the handler is still a stub\n\n```python\ndef handler():\n    pass\n```"
    assert parse_verdict(stub) is False
    go_output = "FAIL\n\nThe fix breaks TestBar.\n\n```\n--- FAIL: TestBar (0.01s)\n--- PASS: TestFoo (0.00s)\n```"
    assert parse_verdict(go_output) is False
    indented_code = "FAIL: the handler is a stub\n\n    def handler():\n        pass"
    assert parse_verdict(indented_code) is False
    quoted = "> FAIL: the previous draft had no test\n\nThe test is there now.\n\nPASS"
    assert parse_verdict(quoted) is True
    table = "| check | result |\n|---|---|\n| tone | PASS |\n\nFAIL: the totals are wrong"
    assert parse_verdict(table) is False
    assert parse_verdict("FAIL\n1. Pass the id to the handler\n2. PASS: n/a") is False
    assert parse_verdict("Assessment done.\n\nFAIL\n  1. Pass the ticket id\n  2. Recompute row 3") is False


def test_only_uppercase_words_state_a_verdict():
    """A capitalized or lowercase 'pass'/'fail' is prose: a line of a fix list, a
    note on behaviour. The first-line reading below still takes a lone 'pass'."""
    assert parse_verdict("PASS\n\nNotes:\n- fail fast on a missing config is implemented as asked") is True
    assert parse_verdict("FAIL\nFix these:\n- the id is wrong\nPass.") is False
    assert parse_verdict("Fail-safe defaults are in place.") is False


def test_a_verdict_on_the_first_line_still_counts():
    """With no verdict line anywhere, the first line is read as before: PASS when it
    starts with 'PASS', so prompts that put the verdict first still route."""
    assert parse_verdict("PASS\nAll criteria met.") is True
    assert parse_verdict("PASS second pass") is True
    assert parse_verdict("FAIL first pass") is False
    assert parse_verdict("FAIL\n- add the missing test\n- rename the helper") is False


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
    assert strip_verdict_line("PASS\nAll good, ship it.", True) == "All good, ship it."


def test_strip_verdict_line_keeps_text_without_verdict():
    from durin.workflow.verdict import strip_verdict_line
    assert strip_verdict_line("just prose", True) == "just prose"


def test_strip_verdict_line_empty_when_only_verdict():
    from durin.workflow.verdict import strip_verdict_line
    assert strip_verdict_line("PASS", True) == ""
    assert strip_verdict_line("**Verdict: FAIL**", False) == ""


def test_strip_verdict_line_removes_a_bare_trailing_verdict():
    from durin.workflow.verdict import strip_verdict_line
    assert strip_verdict_line("Checked all three claims.\n\nPASS", True) == "Checked all three claims."
    assert strip_verdict_line(_JUDGE_ANSWER, True) == _JUDGE_ANSWER.rsplit("\n\nPASS", 1)[0]
    # A criterion line that only mentions a verdict stays.
    assert "CORRECTNESS — PASS" in strip_verdict_line(_JUDGE_ANSWER, True)


def test_strip_verdict_line_never_deletes_content():
    """Only a line that is nothing but the verdict goes: a criterion bullet, a line
    of quoted code or a note that starts with the word is the gate's content."""
    from durin.workflow.verdict import strip_verdict_line
    review = ("## Final review\n\n- PASS: citations resolve\n- FAIL: the expiry date is wrong "
              "(says 30 days, contract says 14)\n\nThe note cannot go out as written.")
    assert strip_verdict_line(review, False) == review
    checks = ("Summary of checks:\nFail-over path: verified\nFail on missing config: raises "
              "ConfigError as required\nAll criteria met.")
    assert strip_verdict_line(checks, True) == checks
    code = "The stub is gone.\n\n```python\ndef handler():\n    pass\n```"
    assert strip_verdict_line(code, True) == code


def test_strip_verdict_line_keeps_a_verdict_that_disagrees_with_the_route():
    """The verdict came from the `route` call: a closing verdict line that says the
    opposite is information, not a redundant marker."""
    from durin.workflow.verdict import strip_verdict_line
    assert strip_verdict_line("The totals look off.\n\nFAIL", True) == "The totals look off.\n\nFAIL"


# --- strip_label_line tests ---


def test_strip_label_line_removes_trailing_label():
    from durin.workflow.verdict import strip_label_line
    assert strip_label_line("The claims are grounded.\n\nGROUNDED", ["GROUNDED", "MISSING"]) \
        == "The claims are grounded."


def test_strip_label_line_tolerates_punctuation():
    from durin.workflow.verdict import strip_label_line
    assert strip_label_line("done\n**GROUNDED**", ["GROUNDED"]) == "done"
