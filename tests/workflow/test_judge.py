"""Tests for the agent judge runner — pick only (routing verdict is now in the node turn)."""

from unittest.mock import AsyncMock, MagicMock

from durin.agent.runner import AgentRunResult, AgentRunner
from durin.providers.base import LLMProvider
from durin.workflow.judge import AgentJudgeRunner


def _judge(final_content):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    runner = AgentRunner(provider)
    runner.run = AsyncMock(return_value=AgentRunResult(final_content=final_content, messages=[]))
    return AgentJudgeRunner(runner, default_model="test-model")


def test_pick_returns_index_of_best_option():
    j = _judge("1\nbecause it is better")
    idx = j.pick("which is best?", ["option 0", "option 1", "option 2"], None)
    assert idx == 1


def test_pick_clamps_out_of_range_to_zero():
    j = _judge("99\nwhatever")
    idx = j.pick("criteria", ["a", "b"], None)
    assert idx == 0


def test_pick_uses_model_override():
    j = _judge("0")
    j.pick("c", ["x"], "pick-model")
    spec = j.runner.run.call_args.args[0]
    assert spec.model == "pick-model"


def test_pick_criteria_and_options_reach_the_prompt():
    j = _judge("0")
    j.pick("MY-CRITERIA", ["OPTION-A", "OPTION-B"], None)
    spec = j.runner.run.call_args.args[0]
    blob = "\n".join(m["content"] for m in spec.initial_messages)
    assert "MY-CRITERIA" in blob
    assert "OPTION-A" in blob
    assert "OPTION-B" in blob
