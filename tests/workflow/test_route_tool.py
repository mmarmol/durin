"""Tests for the `route` tool call (forced end-of-turn and early/mid-turn) in
AgentNodeRunner.

Covers:
1. When provider.chat_with_retry returns a valid route tool call, the engine uses that label as
   the routing verdict — even when the node's text output contains no parseable label.
2. When provider.chat_with_retry raises, the engine gracefully falls back to parsing the node's
   text output (route_label is None), preserving existing behaviour.
3. A valid `route` call anywhere in the work loop decides the verdict unconditionally and skips
   the forced call — exactly symmetric with an early valid `deliver` call (see
   test_node_runner_structured.py). An invalid label is neither captured nor treated as decided.
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
    """When provider.chat_with_retry returns a route tool call with a valid label, the engine
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

    # provider.chat_with_retry is called for the forced route tool call and returns NEED_INFO.
    route_tool_call = SimpleNamespace(
        name="route",
        arguments={"label": "NEED_INFO"},
    )
    route_response = SimpleNamespace(tool_calls=[route_tool_call])
    mock_provider.chat_with_retry = AsyncMock(return_value=route_response)

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
    """When provider.chat_with_retry raises, route_label is None and the engine falls back to
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

    # provider.chat_with_retry raises — the route tool call fails.
    mock_provider.chat_with_retry = AsyncMock(side_effect=Exception("provider unavailable"))

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


# ── `route` rides the node's tool list from turn 1 (cache-prefix preserving) ──


def test_last_valid_route_call_wins(tmp_path):
    """Mirrors deliver's ``test_last_valid_deliver_wins``: the captured label is
    overwritten on every VALID call, so the LAST one decides — even though an
    earlier call in the same turn also passed a valid label. The forced call
    never fires: a valid capture decides the verdict unconditionally."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"
    mock_provider.chat_with_retry = AsyncMock()

    async def fake_run(spec):
        # The engine also dispatches the "worker" node through this same patched
        # run(); only the "gate" node's registry has `route` registered.
        if spec.tools.has("route"):
            await spec.tools.execute("route", {"label": "DECLINE"})
            await spec.tools.execute("route", {"label": "PROCEED"})
        return AgentRunResult(
            final_content="", messages=[{"role": "assistant", "content": ""}],
        )

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        result = engine.run(wf, "help me")

    mock_provider.chat_with_retry.assert_not_called()
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    assert gate_run.route_label == "PROCEED"
    assert any(r.node_id == "worker" for r in result.runs)


def test_forced_route_call_tools_match_the_loop_exactly(tmp_path):
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"

    seen = {}

    async def fake_run(spec):
        # The engine also dispatches the "worker" node through this same patched
        # run(); only the "gate" node's registry has `route` registered.
        if spec.tools.has("route"):
            seen["spec"] = spec
        return AgentRunResult(
            final_content="proceed with it",
            messages=[{"role": "assistant", "content": "proceed with it"}],
        )

    route_response = SimpleNamespace(tool_calls=[SimpleNamespace(arguments={"label": "PROCEED"})])
    mock_provider.chat_with_retry = AsyncMock(return_value=route_response)

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        engine.run(wf, "help me")

    loop_tools = seen["spec"].tools.get_definitions()
    assert "route" in {t["function"]["name"] for t in loop_tools}   # registered from turn 1
    forced_kwargs = mock_provider.chat_with_retry.await_args.kwargs
    assert forced_kwargs["tools"] == loop_tools
    assert forced_kwargs["tool_choice"] == {"type": "function", "function": {"name": "route"}}


# ── a valid route call decides the verdict unconditionally, symmetric with ──
# ── how a valid early `deliver` call ends a schema'd node's turn ────────────


def test_valid_route_call_ends_the_turn_no_forced_call(tmp_path):
    """A valid `route` call decides the verdict immediately, no matter where in
    the turn it happens — exactly symmetric with
    test_early_valid_deliver_ends_the_turn_with_the_payload
    (tests/workflow/test_node_runner_structured.py). The forced end-of-turn
    call is skipped entirely."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"
    # If the forced call fired anyway it would route to DECLINE instead of the
    # captured PROCEED — belt and suspenders alongside assert_not_called().
    mock_provider.chat_with_retry = AsyncMock(
        return_value=SimpleNamespace(tool_calls=[
            SimpleNamespace(arguments={"label": "DECLINE"})]))

    async def fake_run(spec):
        if spec.tools.has("route"):
            ack = await spec.tools.execute("route", {"label": "PROCEED"})
            assert "recorded" in ack.lower()
            return AgentRunResult(
                final_content="", messages=[{"role": "assistant", "content": ""}])
        return AgentRunResult(final_content="done",
                              messages=[{"role": "assistant", "content": "done"}])

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        result = engine.run(wf, "help me")

    mock_provider.chat_with_retry.assert_not_called()
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    assert gate_run.route_label == "PROCEED"
    assert any(r.node_id == "worker" for r in result.runs)


def test_invalid_label_gets_actionable_ack_and_forced_call_still_decides(tmp_path):
    """An invalid label (not one of the node's cases) is neither captured nor
    treated as decided — it gets a specific, actionable ack naming the allowed
    labels, the loop continues, and the end-of-turn forced call remains
    authoritative, exactly as when nothing was ever captured."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"
    mock_provider.chat_with_retry = AsyncMock(
        return_value=SimpleNamespace(tool_calls=[
            SimpleNamespace(arguments={"label": "PROCEED"})]))

    captured = {}

    async def fake_run(spec):
        if spec.tools.has("route"):
            captured["ack"] = await spec.tools.execute("route", {"label": "BOGUS"})
            return AgentRunResult(
                final_content="still working",
                messages=[{"role": "assistant", "content": "still working"}])
        return AgentRunResult(final_content="done",
                              messages=[{"role": "assistant", "content": "done"}])

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        result = engine.run(wf, "help me")

    assert "not one of the allowed labels" in captured["ack"]
    for label in ("NEED_INFO", "PROCEED", "DECLINE"):
        assert label in captured["ack"]
    mock_provider.chat_with_retry.assert_awaited_once()
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    assert gate_run.route_label == "PROCEED"
    assert any(r.node_id == "worker" for r in result.runs)


def test_route_tool_absent_when_node_has_no_routing(tmp_path):
    """A node with no `on_pass`/`on_fail`/`cases` never registers `route` at
    all — unchanged by this fix."""
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "prompt": "p", "next": None}]})

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"
    seen = {}

    async def fake_run(spec):
        seen["tools"] = spec.tools.tool_names
        return AgentRunResult(final_content="done",
                              messages=[{"role": "assistant", "content": "done"}])

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        engine.run(wf, "help me")

    assert "route" not in seen["tools"]
