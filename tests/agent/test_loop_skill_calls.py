from durin.agent.skill_usage import extract_skill_calls


def _record_skill_calls(session_metadata: dict, messages: list[dict]) -> None:
    calls = extract_skill_calls(messages)
    if calls:
        session_metadata.setdefault("skill_calls", []).extend(calls)


def test_recording_appends_to_existing_skill_calls():
    md = {"skill_calls": [{"skill": "old", "op": "read"}]}
    msgs = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "skill_edit", "arguments": {"name": "git-helper"}}}]}]
    _record_skill_calls(md, msgs)
    assert md["skill_calls"] == [{"skill": "old", "op": "read"}, {"skill": "git-helper", "op": "edit"}]


def test_recording_is_noop_when_no_skill_calls():
    md = {}
    _record_skill_calls(md, [{"role": "user", "content": "hi"}])
    assert "skill_calls" not in md
