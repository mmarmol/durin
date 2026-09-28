"""A pruned tool result must be read back, not recalled.

Live, a workflow node that had old results pruned answered with invented
facts for them: the placeholder did not say the content was gone, and the
node's prompt — unlike the chat's operating floor — carried no rule for
getting it back. The placeholder now says so itself, and every agent run
that has tools without the operating floor (subagents, workflow nodes)
carries the recovery rule.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from durin.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode

RULE_MARK = "do not answer from what you remember of it"


def test_the_placeholder_says_the_content_is_gone_and_must_be_read_back(tmp_path: Path) -> None:
    runner = AgentRunner(MagicMock())
    spec = AgentRunSpec(
        initial_messages=[], tools=MagicMock(), model="m", max_iterations=1,
        max_tool_result_chars=16_000, workspace=tmp_path, session_key="sess",
    )
    message = {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "FACT: x\n" * 200}
    placeholder = runner._microcompact_reference(spec, message, 3, message["content"])
    assert "no longer shown here" in placeholder
    assert "before you use anything from it" in placeholder
    assert 'read_file(path="' in placeholder


def test_without_a_workspace_the_placeholder_says_it_was_not_saved() -> None:
    runner = AgentRunner(MagicMock())
    spec = AgentRunSpec(
        initial_messages=[], tools=MagicMock(), model="m", max_iterations=1,
        max_tool_result_chars=16_000,
    )
    message = {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "out\n" * 300}
    placeholder = runner._microcompact_reference(spec, message, 3, message["content"])
    assert "not saved" in placeholder


def _node_system(tmp_path: Path, tools: str) -> str:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    runner = AgentRunner(provider)
    runner.run = AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))
    nr = AgentNodeRunner(runner, SessionManager(workspace=tmp_path), default_model="test-model")
    nr(NodeRunRequest(
        node=WorkNode(id="a", prompt="Do it.", tools=tools, next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    ))
    spec = nr.runner.run.call_args.args[0]
    return next(m["content"] for m in spec.initial_messages if m["role"] == "system")


def test_a_node_with_tools_is_told_how_to_recover_trimmed_results(tmp_path: Path) -> None:
    system = _node_system(tmp_path, "default")
    assert system.startswith("Do it.")
    assert RULE_MARK in system


def test_a_node_without_tools_gets_no_tool_rule(tmp_path: Path) -> None:
    assert _node_system(tmp_path, "none") == "Do it."


def test_a_subagent_is_told_how_to_recover_trimmed_results(tmp_path: Path) -> None:
    from durin.agent.subagent import SubagentManager
    from durin.bus.queue import MessageBus

    manager = SubagentManager(
        provider=MagicMock(), workspace=tmp_path, bus=MessageBus(), max_tool_result_chars=None,
    )
    assert RULE_MARK in manager._build_subagent_prompt()
