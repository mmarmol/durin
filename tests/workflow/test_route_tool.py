"""Tests for the forced `route` tool call in AgentNodeRunner.

Covers two behaviours:
1. When provider.chat returns a valid route tool call, the engine uses that label as
   the routing verdict — even when the node's text output contains no parseable label.
2. When provider.chat raises, the engine gracefully falls back to parsing the node's
   text output (route_label is None), preserving existing behaviour.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.engine import WorkflowEngine
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import parse_workflow


def _make_node_runner(tmp_path, mock_provider):
    """Build an AgentNodeRunner whose AgentRunner uses mock_provider."""
    from durin.agent.runner import AgentRunner

    ar = AgentRunner(mock_provider)
    sessions = SessionManager(workspace=tmp_path)
    return AgentNodeRunner(ar, sessions, default_model="test-model")


def _multi_way_workflow():
    return parse_workflow({
        "name": "triage",
        "start": "gate",
        "nodes": [
            {
                "id": "gate",
                "kind": "work",
                "prompt": "Triage the request.",
                "cases": {
                    "NEED_INFO": None,
                    "PROCEED": "worker",
                    "DECLINE": None,
                },
            },
            {"id": "worker", "kind": "work", "next": None},
        ],
    })


def test_route_tool_verdict_overrides_unparseable_text(tmp_path):
    """When provider.chat returns a route tool call with a valid label, the engine
    must route by that label — even though the node's text output has no parseable label."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"

    # The agent's main turn: returns text with NO clean label so text-parse would fail.
    ambiguous_output = "I think we should continue."
    main_result = AgentRunResult(
        final_content=ambiguous_output,
        messages=[{"role": "assistant", "content": ambiguous_output}],
    )

    # provider.chat is called for the forced route tool call and returns NEED_INFO.
    route_tool_call = SimpleNamespace(
        name="route",
        arguments={"label": "NEED_INFO"},
    )
    route_response = SimpleNamespace(tool_calls=[route_tool_call])
    mock_provider.chat = AsyncMock(return_value=route_response)

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(return_value=main_result)):
        result = engine.run(wf, "help me")

    # NEED_INFO maps to None (terminal) so the workflow ends here.
    assert result.status == "completed"
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    # The engine recorded the route_label in the NodeRun trace.
    assert gate_run.route_label == "NEED_INFO"
    # The worker node must NOT have run (NEED_INFO is a terminal target).
    assert not any(r.node_id == "worker" for r in result.runs)


def test_route_tool_failure_falls_back_to_text_parse(tmp_path):
    """When provider.chat raises, route_label is None and the engine falls back to
    parsing the node's text output — existing behaviour is preserved."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"

    # The agent's main turn: the last line is exactly the label so text-parse matches it.
    parseable_output = "The request looks valid.\nPROCEED"
    main_result = AgentRunResult(
        final_content=parseable_output,
        messages=[{"role": "assistant", "content": parseable_output}],
    )

    # The worker node returns something so the workflow can complete. It has no routing,
    # so _derive_route_label is never called for it — no conflict with the failing mock.
    worker_result = AgentRunResult(
        final_content="done",
        messages=[{"role": "assistant", "content": "done"}],
    )

    # provider.chat raises — the route tool call fails.
    mock_provider.chat = AsyncMock(side_effect=Exception("provider unavailable"))

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    results_iter = iter([main_result, worker_result])
    with patch("durin.agent.runner.AgentRunner.run",
               AsyncMock(side_effect=lambda *a, **k: next(results_iter))):
        result = engine.run(wf, "help me")

    # Text-parse of the output matches "PROCEED" (the last line) and routes to the worker.
    assert result.status == "completed"
    assert any(r.node_id == "worker" for r in result.runs)
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    # route_label is None because the route tool call failed; the engine used text-parse.
    assert gate_run.route_label == "PROCEED"
