from durin.loops.judge import build_filter_prompt, build_prompt, parse_filter_verdict, parse_verdict


def test_parse_verdict_happy():
    v = parse_verdict('noise {"intent_met": true, "assertions": {"a": true, "b": false}} noise')
    assert v == {"intent_met": True, "assertions": {"a": True, "b": False}}


def test_parse_verdict_garbage_defaults_to_not_met():
    assert parse_verdict("no json here") == {"intent_met": False, "assertions": {}}
    assert parse_verdict("") == {"intent_met": False, "assertions": {}}


def test_build_prompt_includes_all_parts():
    p = build_prompt("done", ["a1"], "EVIDENCE")
    assert "done" in p and "- a1" in p and "EVIDENCE" in p


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
