"""The forced ``deliver`` tool: schema-validated node output with in-node retry.

Mirrors the ``route`` verdict machinery, but with no fallback: a schema'd node
that cannot produce a valid payload has failed (typed NodeExecutionError, so
the run aborts naming it and failure-resume can retry it). Providers don't
reliably enforce JSON Schema, so validation is server-side and an invalid
payload is retried immediately with the exact validation error as feedback.
A length-truncated delivery is named as truncation (not misreported as a schema
miss) and a provider-error response fails fast with the provider's message.
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
    def _as_response(item):
        if isinstance(item, SimpleNamespace):        # full response (finish_reason etc.)
            return item
        return SimpleNamespace(tool_calls=[SimpleNamespace(arguments=item)] if item is not None else [])

    provider.chat_with_retry = AsyncMock(side_effect=[_as_response(i) for i in deliver_responses])
    return AgentNodeRunner(ar, SessionManager(workspace=tmp_path), default_model="test-model"), provider


def _req(node):
    return NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[],
                          run_id="r1", iteration=1, root_session_key=None)


def test_valid_payload_first_try_becomes_the_output(tmp_path):
    nr, provider = _runner_with_deliver(tmp_path, [{"queries": ["a", "b"]}])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["a", "b"]}
    assert provider.chat_with_retry.await_count == 1


def test_invalid_payload_is_retried_with_the_validation_error(tmp_path):
    nr, provider = _runner_with_deliver(
        tmp_path, [{"queries": []}, {"queries": ["fixed"]}])   # minItems violation, then valid
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["fixed"]}
    assert provider.chat_with_retry.await_count == 2
    retry_messages = provider.chat_with_retry.await_args_list[1].kwargs["messages"]
    feedback = retry_messages[-1]["content"]
    assert "did not satisfy the output schema" in feedback
    assert "queries" in feedback                    # names where it failed


def test_exhausted_attempts_raise_a_typed_node_failure(tmp_path):
    nr, provider = _runner_with_deliver(
        tmp_path, [{"nope": 1}, {"nope": 2}, {"nope": 3}])
    with pytest.raises(NodeExecutionError) as exc:
        nr(_req(_schema_node()))
    assert "structured output failed" in str(exc.value.cause)
    assert provider.chat_with_retry.await_count == 3


def test_missing_tool_call_counts_as_a_failed_attempt(tmp_path):
    nr, provider = _runner_with_deliver(tmp_path, [None, {"queries": ["ok"]}])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["ok"]}
    retry_messages = provider.chat_with_retry.await_args_list[1].kwargs["messages"]
    assert "no deliver tool call" in retry_messages[-1]["content"]


def test_truncated_invalid_payload_names_the_output_limit_and_recovers(tmp_path):
    truncated = SimpleNamespace(
        tool_calls=[SimpleNamespace(arguments={"queries": []})], finish_reason="length")
    nr, provider = _runner_with_deliver(tmp_path, [truncated, {"queries": ["ok"]}])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["ok"]}
    feedback = provider.chat_with_retry.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "output-token limit" in feedback
    assert "did not satisfy the output schema" not in feedback


def test_all_attempts_truncated_report_truncation_not_schema(tmp_path):
    def t():
        return SimpleNamespace(tool_calls=[SimpleNamespace(arguments={})], finish_reason="length")
    nr, provider = _runner_with_deliver(tmp_path, [t(), t(), t()])
    with pytest.raises(NodeExecutionError) as exc:
        nr(_req(_schema_node()))
    msg = str(exc.value.cause)
    assert "output-token limit" in msg
    assert "is a required property" not in msg
    assert provider.chat_with_retry.await_count == 3


def test_provider_error_response_fails_fast_with_the_provider_message(tmp_path):
    err = SimpleNamespace(tool_calls=[], finish_reason="error",
                          content="Error calling LLM: boom")
    nr, provider = _runner_with_deliver(tmp_path, [err])
    with pytest.raises(NodeExecutionError) as exc:
        nr(_req(_schema_node()))
    assert "provider error" in str(exc.value.cause)
    assert "boom" in str(exc.value.cause)
    assert provider.chat_with_retry.await_count == 1, "must not burn attempts on a dead provider"


def test_complete_valid_payload_is_accepted_even_when_flagged_length(tmp_path):
    r = SimpleNamespace(tool_calls=[SimpleNamespace(arguments={"queries": ["a"]})],
                        finish_reason="length")
    nr, provider = _runner_with_deliver(tmp_path, [r])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["a"]}


# ── `deliver` rides the node's tool list from turn 1 (cache-prefix preserving) ──


def _no_schema_node():
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "prompt": "Plan.", "next": None},
    ]})
    return wf.nodes["plan"]


def _runner_with_fake_run(tmp_path, fake_run, chat_responses=()):
    """An AgentNodeRunner whose AgentRunner.run is a caller-supplied coroutine
    function — used where the fake needs to touch ``spec.tools`` itself (to
    simulate the model calling a tool mid-loop), unlike ``_runner_with_deliver``
    which only ever returns a canned result."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    ar = AgentRunner(provider)
    ar.run = AsyncMock(side_effect=fake_run)
    provider.chat_with_retry = AsyncMock(side_effect=list(chat_responses)) if chat_responses else AsyncMock()
    return AgentNodeRunner(ar, SessionManager(workspace=tmp_path), default_model="test-model"), provider


def test_deliver_tool_is_registered_when_schema_declared(tmp_path):
    seen = {}

    async def fake_run(spec):
        seen["tools"] = spec.tools.tool_names
        await spec.tools.execute("deliver", {"queries": ["a"]})   # deliver early so no forced call is needed
        return AgentRunResult(final_content="", messages=[{"role": "user", "content": "t"}])

    nr, _ = _runner_with_fake_run(tmp_path, fake_run)
    nr(_req(_schema_node()))
    assert "deliver" in seen["tools"]


def test_deliver_tool_absent_when_no_schema_declared(tmp_path):
    seen = {}

    async def fake_run(spec):
        seen["tools"] = spec.tools.tool_names
        return AgentRunResult(final_content="done", messages=[{"role": "user", "content": "t"}])

    nr, _ = _runner_with_fake_run(tmp_path, fake_run)
    nr(_req(_no_schema_node()))
    assert "deliver" not in seen["tools"]


def test_early_valid_deliver_ends_the_turn_with_the_payload(tmp_path):
    async def fake_run(spec):
        await spec.tools.execute("deliver", {"queries": ["a", "b"]})
        return AgentRunResult(final_content="", messages=[{"role": "user", "content": "t"}])

    nr, provider = _runner_with_fake_run(tmp_path, fake_run)
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["a", "b"]}
    provider.chat_with_retry.assert_not_called()   # no forced end-of-turn call


def test_early_invalid_deliver_returns_validation_error_and_continues(tmp_path):
    captured = {}

    async def fake_run(spec):
        captured["result"] = await spec.tools.execute("deliver", {"queries": []})  # minItems violation
        return AgentRunResult(final_content="prose", messages=[{"role": "user", "content": "t"}])

    nr, provider = _runner_with_fake_run(
        tmp_path, fake_run,
        chat_responses=[SimpleNamespace(tool_calls=[SimpleNamespace(arguments={"queries": ["fixed"]})])])
    resp = nr(_req(_schema_node()))
    assert "did not satisfy the output schema" in captured["result"]
    assert "keep working" in captured["result"].lower()
    assert json.loads(resp.output) == {"queries": ["fixed"]}   # the forced call still ran and decided it
    provider.chat_with_retry.assert_awaited_once()


def test_forced_deliver_sends_full_tool_list(tmp_path):
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "tools": "default", "prompt": "Plan.",
         "output_schema": SCHEMA, "next": None},
    ]})
    node = wf.nodes["plan"]
    seen = {}

    async def fake_run(spec):
        seen["spec"] = spec
        return AgentRunResult(final_content="prose", messages=[{"role": "user", "content": "t"}])

    nr, provider = _runner_with_fake_run(
        tmp_path, fake_run,
        chat_responses=[SimpleNamespace(tool_calls=[SimpleNamespace(arguments={"queries": ["a"]})])])
    nr(_req(node))

    loop_tools = seen["spec"].tools.get_definitions()
    assert len(loop_tools) > 1, "a real tool set, not just deliver alone"
    forced_kwargs = provider.chat_with_retry.await_args.kwargs
    assert forced_kwargs["tools"] == loop_tools
    assert forced_kwargs["tool_choice"] == {"type": "function", "function": {"name": "deliver"}}
