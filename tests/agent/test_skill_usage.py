from durin.agent.skill_usage import extract_skill_calls


def test_read_file_on_skill_md_is_a_read_call():
    messages = [{"role": "assistant", "tool_calls": [
        {"id": "1", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path": "skills/git-helper/SKILL.md"}'}},
    ]}]
    assert extract_skill_calls(messages) == [{"skill": "git-helper", "op": "read"}]


def test_skill_edit_is_an_edit_call():
    messages = [{"role": "assistant", "tool_calls": [
        {"id": "2", "type": "function", "function": {
            "name": "skill_edit", "arguments": {"name": "deploy-flow", "old": "a", "new": "b"}}},
    ]}]
    assert extract_skill_calls(messages) == [{"skill": "deploy-flow", "op": "edit"}]


def test_non_skill_read_and_other_tools_are_ignored():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": '{"path": "src/main.py"}'}},
            {"function": {"name": "grep", "arguments": '{"pattern": "x"}'}},
        ]},
        {"role": "user", "content": "hi"},
    ]
    assert extract_skill_calls(messages) == []
