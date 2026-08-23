"""WorkflowsService's service-path progress publisher (Task A5).

Runs launched through the service path (loops today, automations soon, a raw
HTTP launch) have no calling chat/session the way an agent-launched
`run_workflow` does, so there is nothing for the engine's per-node frames to
attach to. `progress_publish` gives WorkflowsService.execute() somewhere to
hand those frames: each engine frame is rewrapped as
{run_id, workflow, task, nodes, done} — mirroring the SHAPE the engine hands
the callback, not reinterpreting it, plus the run's own task text (capped like
a run manifest's own `task` field) — and handed to the publisher. With no
publisher configured (the default), execute() must behave exactly as every
other WorkflowsService test already assumes.

`build_runs_feed_event` is the pure function the gateway wiring
(durin/cli/commands.py) uses to turn one of these payloads into the
`workflow_progress` websocket event — the exact schema
durin/agent/tools/run_workflow.py's per-chat progress publisher builds, so the
existing work-panel renderer can consume a runs:feed frame unchanged. It is
tested directly here; the thread-to-loop marshalling around it
(asyncio.run_coroutine_threadsafe) is gateway wiring, not exercised by this
file.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.providers.base import GenerationSettings, LLMProvider
from durin.service.workflows import WorkflowsService, build_runs_feed_event
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

    result = await _run(service, "w1", task="do the thing")

    assert result.status == "completed"
    assert frames, "expected at least one progress frame during the run"
    for frame in frames:
        assert set(frame) >= {"run_id", "workflow", "task", "nodes", "done"}
        assert frame["run_id"] == result.run_id
        assert frame["workflow"] == "w1"
        assert frame["task"] == "do the thing"
        assert frame["done"] is False


@pytest.mark.asyncio
async def test_no_progress_publisher_by_default_does_not_break_execute(tmp_path):
    _write_two_node_workflow(tmp_path, "w2")
    service = _svc(tmp_path)  # progress_publish defaults to None

    result = await _run(service, "w2")

    assert result.status == "completed"


def test_build_runs_feed_event_mirrors_the_run_workflow_schema():
    """Same six keys, same field names, as run_workflow.py's own progress
    publisher (durin/agent/tools/run_workflow.py) — so a runs:feed frame and a
    per-chat frame render through the identical work-panel code."""
    payload = {
        "run_id": "abc123", "workflow": "w1", "task": "chase invoice",
        "nodes": [{"id": "n1", "status": "running"}], "done": False,
    }

    ev = build_runs_feed_event(payload)

    assert set(ev) == {"version", "phase", "call_id", "name", "arguments", "nodes"}
    assert ev["version"] == 1
    assert ev["phase"] == "running"
    assert ev["call_id"] == "workflow:abc123"
    assert ev["name"] == "workflow_progress"
    assert ev["arguments"] == {"workflow": "w1", "task": "chase invoice"}
    assert ev["nodes"] == payload["nodes"]


def test_build_runs_feed_event_phase_is_end_when_done():
    payload = {"run_id": "abc123", "workflow": "w1", "task": "", "nodes": [], "done": True}

    ev = build_runs_feed_event(payload)

    assert ev["phase"] == "end"


def test_build_runs_feed_event_defaults_task_when_absent():
    """Belt and suspenders: the payload contract always carries `task`, but the
    builder itself does not assume that — matching payload.get, not payload[]."""
    payload = {"run_id": "abc123", "workflow": "w1", "nodes": [], "done": False}

    ev = build_runs_feed_event(payload)

    assert ev["arguments"]["task"] == ""
