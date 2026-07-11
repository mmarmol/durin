from durin.loops.judge import build_prompt, parse_verdict


def test_parse_verdict_happy():
    v = parse_verdict('noise {"intent_met": true, "assertions": {"a": true, "b": false}} noise')
    assert v == {"intent_met": True, "assertions": {"a": True, "b": False}}


def test_parse_verdict_garbage_defaults_to_not_met():
    assert parse_verdict("no json here") == {"intent_met": False, "assertions": {}}
    assert parse_verdict("") == {"intent_met": False, "assertions": {}}


def test_build_prompt_includes_all_parts():
    p = build_prompt("done", ["a1"], "EVIDENCE")
    assert "done" in p and "- a1" in p and "EVIDENCE" in p
