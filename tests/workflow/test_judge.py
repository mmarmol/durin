"""Tests for the agent judge runner (reviewer verdict on a node's output)."""

from unittest.mock import AsyncMock, MagicMock

from durin.agent.runner import AgentRunResult, AgentRunner
from durin.providers.base import LLMProvider
from durin.workflow.judge import AgentJudgeRunner, JudgeVerdict


def _judge(final_content):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    runner = AgentRunner(provider)
    runner.run = AsyncMock(return_value=AgentRunResult(final_content=final_content, messages=[]))
    return AgentJudgeRunner(runner, default_model="test-model")


def test_pass_verdict():
    j = _judge("PASS\nLooks correct.")
    v = j("Is it correct?", "the work", None)
    assert isinstance(v, JudgeVerdict)
    assert v.passed is True


def test_fail_verdict_keeps_feedback():
    j = _judge("FAIL\nMissing error handling on the parse path.")
    v = j("Is it correct?", "the work", None)
    assert v.passed is False
    assert "error handling" in v.feedback


def test_criteria_and_output_reach_the_judge_prompt():
    j = _judge("PASS")
    j("MY-CRITERIA", "MY-OUTPUT", None)
    spec = j.runner.run.call_args.args[0]
    blob = "\n".join(m["content"] for m in spec.initial_messages)
    assert "MY-CRITERIA" in blob
    assert "MY-OUTPUT" in blob


def test_judge_model_override():
    j = _judge("PASS")
    j("c", "o", "review-model")
    spec = j.runner.run.call_args.args[0]
    assert spec.model == "review-model"
