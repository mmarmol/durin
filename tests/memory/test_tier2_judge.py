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
