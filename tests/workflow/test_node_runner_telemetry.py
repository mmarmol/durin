"""Workflow nodes bind their own session telemetry.

``AgentNodeRunner.__call__`` binds ``get_session_logger(<node session key>)``
around the whole node execution, so every event a node produces — tool events
from its work loop, ``provider.call`` from its LLM round-trips — lands in a
``workflow_<run>_<node>`` telemetry file instead of vanishing (before this,
workflow runs emitted zero telemetry: the 2026-08-19 z.ai audit found them
to be the largest invisible consumer).
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import durin.telemetry.logger as telemetry_logger
from durin.agent.runner import AgentRunResult, AgentRunner
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.telemetry.logger import current_telemetry
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import parse_workflow

SCHEMA = {
    "type": "object",
    "required": ["queries"],
    "properties": {"queries": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
}


def _node(schema=None):
    spec = {"id": "plan", "kind": "work", "prompt": "Plan.", "next": None}
    if schema is not None:
        spec["output_schema"] = schema
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [spec]})
    return wf.nodes["plan"]


def _req(node):
    return NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[],
                          run_id="r1", iteration=1, root_session_key=None)


def _runner(tmp_path, deliver_responses=None, on_run=None):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    ar = AgentRunner(provider)

    async def _fake_run(spec):
        if on_run is not None:
            on_run()
        return AgentRunResult(
            final_content="prose answer",
            messages=[{"role": "user", "content": "t"},
                      {"role": "assistant", "content": "prose answer"}],
        )

    ar.run = AsyncMock(side_effect=_fake_run)
    if deliver_responses is not None:
        provider.chat_with_retry = AsyncMock(side_effect=[
            SimpleNamespace(tool_calls=[SimpleNamespace(arguments=args)] if args is not None else [])
            for args in deliver_responses
        ])
    return AgentNodeRunner(ar, SessionManager(workspace=tmp_path), default_model="test-model"), provider


def test_node_execution_binds_the_node_session_logger(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry_logger, "_DEFAULT_DIR", tmp_path / "telemetry")
    seen: list = []
    nr, _ = _runner(tmp_path, on_run=lambda: seen.append(current_telemetry()))
    node = _node()
    req = _req(node)
    expected_key = nr._session_key(req)

    assert current_telemetry() is None
    nr(req)
    assert current_telemetry() is None, "binding must not leak past the node"
    assert len(seen) == 1 and seen[0] is not None, "work loop ran without a bound logger"
    assert seen[0].session_key == expected_key


def test_node_events_land_in_a_workflow_telemetry_file(tmp_path, monkeypatch):
    tel_dir = tmp_path / "telemetry"
    monkeypatch.setattr(telemetry_logger, "_DEFAULT_DIR", tel_dir)

    def _emit_from_work_loop():
        current_telemetry().log("tool.read_file", {"path": "x", "offset": 1, "limit": 1,
                                                   "total_lines": 1, "returned_lines": 1})

    nr, _ = _runner(tmp_path, on_run=_emit_from_work_loop)
    nr(_req(_node()))
    files = list(tel_dir.glob("workflow_r1_plan*.jsonl"))
    assert len(files) == 1, f"expected one node telemetry file, got {files}"
    events = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert any(e["type"] == "tool.read_file" for e in events)


def test_structured_output_goes_through_the_retry_wrapper(tmp_path, monkeypatch):
    """Deliver calls ride chat_with_retry: transport retries, generation-resolved
    max_tokens, and provider.call telemetry emitted by the wrapper itself."""
    monkeypatch.setattr(telemetry_logger, "_DEFAULT_DIR", tmp_path / "telemetry")
    nr, provider = _runner(tmp_path, deliver_responses=[{"queries": ["a"]}])
    nr(_req(_node(schema=SCHEMA)))
    provider.chat_with_retry.assert_awaited_once()
    provider.chat.assert_not_called()
