"""WorkflowsService's service-path progress publisher (Task A5).

Runs launched through the service path (loops today, automations soon, a raw
HTTP launch) have no calling chat/session the way an agent-launched
`run_workflow` does, so there is nothing for the engine's per-node frames to
attach to. `progress_publish` gives WorkflowsService.execute() somewhere to
hand those frames: each engine frame is rewrapped as
{run_id, workflow, nodes, done} — mirroring the SHAPE the engine hands the
callback, not reinterpreting it — and handed to the publisher. The gateway
wiring (durin/cli/commands.py) turns each of these into a `runs:feed`
websocket event; that marshalling is exercised only by the gateway wiring
itself, not here. With no publisher configured (the default), execute() must
behave exactly as every other WorkflowsService test already assumes.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.providers.base import GenerationSettings, LLMProvider
from durin.service.workflows import WorkflowsService
from durin.session.manager import SessionManager
from durin.workflow.loader import workflows_dir


def _svc(tmp_path, **kwargs):
    app_config = SimpleNamespace(
        resolve_default_preset=lambda: object(),
        tools=ToolsConfig(),
        workflow=WorkflowConfig(),
    )
    return WorkflowsService(workspace=tmp_path, app_config=app_config,
                            sessions=SessionManager(workspace=tmp_path), **kwargs)


def _write_two_node_workflow(tmp_path, name):
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name, "start": "n1",
        "nodes": [
            {"id": "n1", "kind": "work", "next": "n2"},
            {"id": "n2", "kind": "work", "next": None},
        ],
    }), encoding="utf-8")


async def _run(service, name, task="do the thing"):
    """Run the workflow for real through the service, faking only the LLM turn
    itself (AgentRunner.run) — same seam test_workflows_approval.py uses."""
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    fake_provider.provider_key = "test-provider"
    fake_provider.generation = GenerationSettings()
    fake_result = AgentRunResult(final_content="done",
                                 messages=[{"role": "assistant", "content": "done"}])
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.runner.AgentRunner.run", AsyncMock(return_value=fake_result)):
        return await service.execute(name, task)


@pytest.mark.asyncio
async def test_progress_publish_gets_a_frame_per_node_event(tmp_path):
    _write_two_node_workflow(tmp_path, "w1")
    frames: list[dict] = []
    service = _svc(tmp_path, progress_publish=frames.append)

    result = await _run(service, "w1")

    assert result.status == "completed"
    assert frames, "expected at least one progress frame during the run"
    for frame in frames:
        assert set(frame) >= {"run_id", "workflow", "nodes", "done"}
        assert frame["run_id"] == result.run_id
        assert frame["workflow"] == "w1"
        assert frame["done"] is False


@pytest.mark.asyncio
async def test_no_progress_publisher_by_default_does_not_break_execute(tmp_path):
    _write_two_node_workflow(tmp_path, "w2")
    service = _svc(tmp_path)  # progress_publish defaults to None

    result = await _run(service, "w2")

    assert result.status == "completed"
