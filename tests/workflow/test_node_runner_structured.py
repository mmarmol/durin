"""The forced ``deliver`` tool: schema-validated node output with in-node retry.

Mirrors the ``route`` verdict machinery, but with no fallback: a schema'd node
that cannot produce a valid payload has failed (typed NodeExecutionError, so
the run aborts naming it and failure-resume can retry it). Providers don't
reliably enforce JSON Schema, so validation is server-side and an invalid
payload is retried immediately with the exact validation error as feedback.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.runner import AgentRunResult, AgentRunner
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeExecutionError, NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import parse_workflow

SCHEMA = {
    "type": "object",
    "required": ["queries"],
    "properties": {"queries": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
}


def _schema_node():
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "prompt": "Plan.",
         "output_schema": SCHEMA, "next": None},
    ]})
    return wf.nodes["plan"]


def _runner_with_deliver(tmp_path, deliver_responses):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    ar = AgentRunner(provider)
    ar.run = AsyncMock(return_value=AgentRunResult(
        final_content="prose answer",
        messages=[{"role": "user", "content": "t"},
                  {"role": "assistant", "content": "prose answer"}],
    ))
    provider.chat = AsyncMock(side_effect=[
        SimpleNamespace(tool_calls=[SimpleNamespace(arguments=args)] if args is not None else [])
        for args in deliver_responses
    ])
    return AgentNodeRunner(ar, SessionManager(workspace=tmp_path), default_model="test-model"), provider


def _req(node):
    return NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[],
                          run_id="r1", iteration=1, root_session_key=None)


def test_valid_payload_first_try_becomes_the_output(tmp_path):
    nr, provider = _runner_with_deliver(tmp_path, [{"queries": ["a", "b"]}])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["a", "b"]}
    assert provider.chat.await_count == 1


def test_invalid_payload_is_retried_with_the_validation_error(tmp_path):
    nr, provider = _runner_with_deliver(
        tmp_path, [{"queries": []}, {"queries": ["fixed"]}])   # minItems violation, then valid
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["fixed"]}
    assert provider.chat.await_count == 2
    retry_messages = provider.chat.await_args_list[1].kwargs["messages"]
    feedback = retry_messages[-1]["content"]
    assert "did not satisfy the output schema" in feedback
    assert "queries" in feedback                    # names where it failed


def test_exhausted_attempts_raise_a_typed_node_failure(tmp_path):
    nr, provider = _runner_with_deliver(
        tmp_path, [{"nope": 1}, {"nope": 2}, {"nope": 3}])
    with pytest.raises(NodeExecutionError) as exc:
        nr(_req(_schema_node()))
    assert "structured output failed" in str(exc.value.cause)
    assert provider.chat.await_count == 3


def test_missing_tool_call_counts_as_a_failed_attempt(tmp_path):
    nr, provider = _runner_with_deliver(tmp_path, [None, {"queries": ["ok"]}])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["ok"]}
    retry_messages = provider.chat.await_args_list[1].kwargs["messages"]
    assert "no deliver tool call" in retry_messages[-1]["content"]
