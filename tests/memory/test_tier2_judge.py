"""Tests for the Tier-2 sub-agent judge (Task 5)."""
import asyncio
from durin.memory import tier2_judge


def test_escalate_judge_parses_agent_verdict(tmp_path, monkeypatch):
    class _FakeResult:
        final_content = ("===VERDICT===\nsame\n===CONFIDENCE===\n96\n"
                         "===REASONING===\nshared warning zone\n===END===")
        tool_events = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            names = set(spec.tools._tools.keys())
            assert {"memory_read_entity", "memory_entity_lineage",
                    "memory_source_session"} <= names
            return _FakeResult()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    j = tier2_judge.escalate_judge(tmp_path, "place:torrent", "place:torrent-valencia")
    assert j.verdict == "same" and j.confidence == 96


def test_escalate_judge_parses_different_verdict(tmp_path, monkeypatch):
    class _FakeResult:
        final_content = ("===VERDICT===\ndifferent\n===CONFIDENCE===\n85\n"
                         "===REASONING===\ndistinct homonyms\n===END===")
        tool_events = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            return _FakeResult()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    j = tier2_judge.escalate_judge(tmp_path, "person:a", "person:b")
    assert j.verdict == "different" and j.confidence == 85


# ---------------------------------------------------------------------------
# The reserved final-answer step
# ---------------------------------------------------------------------------

_ENVELOPE = ("===VERDICT===\nsame\n===CONFIDENCE===\n84\n"
             "===REASONING===\nsame slug lineage\n===END===")

_MAX_ITER_PLACEHOLDER = ("I reached the maximum number of tool call iterations (6) "
                         "without completing the task.")


def _investigation_messages():
    """What the runner hands back when the agent spent every iteration on tools."""
    return [
        {"role": "user", "content": "Decide whether these two memory entities..."},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "memory_read_entity",
                                      "arguments": '{"ref": "artifact:auth-page"}'}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "artifact:auth-page — the login page served by the auth SPA"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "type": "function",
                         "function": {"name": "memory_entity_lineage",
                                      "arguments": '{"ref": "artifact:auth-spa"}'}}]},
        {"role": "tool", "tool_call_id": "c2",
         "content": "artifact:auth-spa — created 2026-07-17 from session slack_C0A"},
        {"role": "assistant", "content": _MAX_ITER_PLACEHOLDER},
    ]


class _ExhaustedResult:
    final_content = _MAX_ITER_PLACEHOLDER
    stop_reason = "max_iterations"
    messages = _investigation_messages()
    tool_events = []


def test_escalate_judge_takes_a_final_answer_step_when_the_agent_runs_out_of_iterations(
        tmp_path, monkeypatch):
    """Live, every escalation of a pair whose investigation needed more than six
    tool iterations ended as `missing ===VERDICT=== block`: the runner's
    max-iterations text has no envelope. The judge must then spend ONE more
    call with no tools, handing the model its own investigation notes, and
    read the envelope from that."""
    specs = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            specs.append(spec)
            if len(specs) == 1:
                return _ExhaustedResult()

            class _Final:
                final_content = _ENVELOPE
                stop_reason = "completed"
                messages = []
                tool_events = []
            return _Final()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    j = tier2_judge.escalate_judge(tmp_path, "artifact:auth-page", "artifact:auth-spa")
    assert j.verdict == "same" and j.confidence == 84
    assert len(specs) == 2
    final = specs[1]
    assert final.tools._tools == {}, "the final step must not offer tools"
    assert final.max_iterations == 1
    assert len(final.initial_messages) == 1 and final.initial_messages[0]["role"] == "user"
    brief = final.initial_messages[0]["content"]
    assert "===VERDICT===" in brief and "===END===" in brief
    assert "artifact:auth-page" in brief and "artifact:auth-spa" in brief
    # The notes carry what the agent read, not only what it asked for.
    assert "login page served by the auth SPA" in brief
    assert "created 2026-07-17" in brief
    assert _MAX_ITER_PLACEHOLDER not in brief


def test_escalate_judge_raises_when_the_final_answer_still_lacks_the_envelope(
        tmp_path, monkeypatch):
    import pytest

    from durin.memory.absorb_judge import JudgeError
    runs = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            runs.append(spec)

            class _R:
                final_content = ("I think they are probably the same thing"
                                 if len(runs) == 2 else _MAX_ITER_PLACEHOLDER)
                stop_reason = "completed" if len(runs) == 2 else "max_iterations"
                messages = _investigation_messages()
                tool_events = []
            return _R()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    with pytest.raises(JudgeError):
        tier2_judge.escalate_judge(tmp_path, "person:a", "person:b")
    assert len(runs) == 2, "exactly one final-answer step, never a loop"


def test_escalate_judge_does_not_spend_a_final_step_when_the_agent_answered(
        tmp_path, monkeypatch):
    runs = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            runs.append(spec)

            class _R:
                final_content = _ENVELOPE
                stop_reason = "completed"
                messages = []
                tool_events = []
            return _R()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    tier2_judge.escalate_judge(tmp_path, "person:a", "person:b")
    assert len(runs) == 1
