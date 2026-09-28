import os
import time

from durin.agent.skill_usage import collect_recent_skill_calls, extract_skill_calls
from durin.session.manager import SessionManager


def test_read_file_on_skill_md_is_a_read_call():
    messages = [{"role": "assistant", "tool_calls": [
        {"id": "1", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path": "skills/git-helper/SKILL.md"}'}},
    ]}]
    assert extract_skill_calls(messages) == [{"skill": "git-helper", "op": "read", "turn": 1}]


def test_skill_edit_is_an_edit_call():
    messages = [{"role": "assistant", "tool_calls": [
        {"id": "2", "type": "function", "function": {
            "name": "skill_edit", "arguments": {"name": "deploy-flow", "old": "a", "new": "b"}}},
    ]}]
    assert extract_skill_calls(messages) == [{"skill": "deploy-flow", "op": "edit", "turn": 1}]


def test_skill_call_records_the_turn_index():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file",
                          "arguments": '{"path": "skills/git-helper/SKILL.md"}'}}]},
    ]
    assert extract_skill_calls(messages) == [
        {"skill": "git-helper", "op": "read", "turn": 2}]


def _exec_turn(command: str, output: str) -> list[dict]:
    return [
        {"role": "assistant", "tool_calls": [
            {"id": "e1", "type": "function",
             "function": {"name": "exec", "arguments": {"command": command}}}]},
        {"role": "tool", "tool_call_id": "e1", "name": "exec", "content": output},
    ]


def test_running_a_skill_script_is_a_run_call_with_its_outcome():
    """A skill's script failing again and again must be visible as use of that
    skill; before, 31 failed runs of one script left no trace."""
    failed = _exec_turn(
        "python3 skills/athena-logs/scripts/athena_query.py --database logs_dev",
        "ValueError: Waiter does not exist\n\nExit code: 1")
    ok = _exec_turn(
        "python3 /home/durin/.durin/workspace/skills/athena-boto3-query/scripts/athena_query.py --sql -",
        "[{\"tab_name\": \"containers\"}]\n\nExit code: 0")

    assert extract_skill_calls(failed) == [
        {"skill": "athena-logs", "op": "run", "turn": 1, "ok": False}]
    assert extract_skill_calls(ok) == [
        {"skill": "athena-boto3-query", "op": "run", "turn": 1, "ok": True}]


def test_printing_a_skill_file_with_exec_is_not_a_run():
    turn = _exec_turn("cat skills/athena-logs/SKILL.md", "---\nname: athena-logs\n\nExit code: 0")
    assert extract_skill_calls(turn) == []


def test_the_signal_pass_sees_a_failed_skill_script():
    from durin.agent.skill_signals import build_skill_signal_prompt

    prompt = build_skill_signal_prompt(
        "the turns", [{"skill": "athena-logs", "op": "run", "turn": 4, "ok": False}])

    assert "athena-logs@4 (script failed)" in prompt


def test_non_skill_read_and_other_tools_are_ignored():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": '{"path": "src/main.py"}'}},
            {"function": {"name": "grep", "arguments": '{"pattern": "x"}'}},
        ]},
        {"role": "user", "content": "hi"},
    ]
    assert extract_skill_calls(messages) == []


def test_collect_recent_skill_calls_aggregates_across_sessions(tmp_path):
    sm = SessionManager(tmp_path)
    for key, skill in (("websocket:a", "git-helper"), ("websocket:b", "git-helper")):
        s = sm.get_or_create(key)
        s.add_message("user", "x")
        s.metadata["skill_calls"] = [{"skill": skill, "op": "read"}]
        sm.save(s)
    agg = collect_recent_skill_calls(tmp_path)
    assert agg.get("git-helper", {}).get("read") == 2


def test_collect_skill_calls_within_hours_filters_old_sidecars(tmp_path):
    sm = SessionManager(tmp_path)
    for key in ("websocket:recent", "websocket:old"):
        s = sm.get_or_create(key)
        s.add_message("user", "x")
        s.metadata["skill_calls"] = [{"skill": "git-helper", "op": "read"}]
        sm.save(s)
    old_meta = next(p for p in (tmp_path / "sessions").glob("*old*.meta.json"))
    old = time.time() - 100 * 3600
    os.utime(old_meta, (old, old))

    assert collect_recent_skill_calls(tmp_path)["git-helper"]["read"] == 2
    assert collect_recent_skill_calls(tmp_path, within_hours=48)["git-helper"]["read"] == 1


# -- telemetry -----------------------------------------------------------------


def test_emit_skill_used_emits_one_event_per_call(monkeypatch):
    import durin.agent.tools._telemetry as tel
    from durin.agent.skill_usage import emit_skill_used

    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    calls = [{"skill": "git-helper", "op": "read", "turn": 1},
             {"skill": "deploy-flow", "op": "edit", "turn": 2}]
    emit_skill_used(calls)
    used = [(n, d) for n, d in events if n == "skill.used"]
    assert used == [
        ("skill.used", {"skill": "git-helper", "op": "read", "turn": 1}),
        ("skill.used", {"skill": "deploy-flow", "op": "edit", "turn": 2}),
    ]


def test_emit_skill_used_noop_on_empty_list(monkeypatch):
    import durin.agent.tools._telemetry as tel
    from durin.agent.skill_usage import emit_skill_used

    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    emit_skill_used([])
    assert events == []
