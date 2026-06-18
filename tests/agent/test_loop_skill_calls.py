from durin.agent.skill_usage import extract_skill_calls


def _record(metadata, all_messages, save_skip):
    # EXACT expression used in loop._state_save (keep in sync).
    new = all_messages[save_skip:]
    calls = extract_skill_calls(new)
    if calls:
        metadata.setdefault("skill_calls", []).extend(calls)


def _assistant_read(skill):
    return {"role": "assistant", "tool_calls": [
        {"function": {"name": "read_file",
                      "arguments": {"path": f"skills/{skill}/SKILL.md"}}}]}


def test_only_new_turn_messages_are_recorded_no_reaccumulation():
    md = {}
    # Turn 1: history empty. all_messages = [user, assistant(read X)]. save_skip=1
    t1 = [{"role": "user", "content": "do X"}, _assistant_read("git-helper")]
    _record(md, t1, save_skip=1)
    # Turn 2: prior turn is now history; all_messages carries it again + new turn.
    t2 = t1 + [{"role": "user", "content": "do Y"}, _assistant_read("deploy-flow")]
    # save_skip excludes everything from turn 1 (its 2 msgs) + the 1 base offset = 3
    _record(md, t2, save_skip=3)
    # `turn` is relative to each save-slice (the loop records only new turns),
    # so both read at slice-local turn 1. Persisted `turn` is unused by the
    # usage consumers (they count by skill/op); the hindsight skill-signal pass
    # recomputes extract_skill_calls over the full post-cursor window instead.
    assert md["skill_calls"] == [
        {"skill": "git-helper", "op": "read", "turn": 1},
        {"skill": "deploy-flow", "op": "read", "turn": 1},
    ]
    # git-helper recorded exactly once, NOT re-counted on turn 2.


def test_no_skill_calls_is_noop():
    md = {}
    _record(md, [{"role": "user", "content": "hi"}], save_skip=1)
    assert "skill_calls" not in md
