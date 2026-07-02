"""Tests for run_workflow background execution mode."""

import asyncio
import pytest
from durin.agent.tools.run_workflow import RunWorkflowTool, _background_launch_message


def test_background_launch_message_carries_run_id_and_points_at_tasks():
    msg = _background_launch_message("research-to-answer", "abc123def456")
    assert "abc123def456" in msg            # the agent gets the id it needs to poll/stop
    assert "research-to-answer" in msg
    assert "tasks(action='status'" in msg   # the structural fix: launch tells it the door
    assert "tasks(action='stop'" in msg


class _FakeBus:
    def __init__(self):
        self.inbound = []

    async def publish_inbound(self, msg):
        self.inbound.append(msg)


def _tool(bus):
    t = RunWorkflowTool(workspace="/tmp/x", sessions=object(), app_config=object(), bus=bus)
    return t


@pytest.mark.asyncio
async def test_inject_result_builds_system_message_routed_to_parent_session():
    bus = _FakeBus()
    t = _tool(bus)
    await t._inject_result(
        "Workflow run r1: completed\nFinal output:\n42",
        name="qa",
        inject_target={"channel": "websocket", "chat_id": "chatA", "session_key": "websocket:chatA"},
    )
    assert len(bus.inbound) == 1
    msg = bus.inbound[0]
    assert msg.channel == "system"
    assert msg.session_key_override == "websocket:chatA"   # routes to the parent's pending queue
    assert "42" in msg.content
    assert msg.metadata.get("injected_event") == "workflow_background_result"
    assert msg.metadata.get("workflow") == "qa"


@pytest.mark.asyncio
async def test_inject_result_is_best_effort_when_bus_missing():
    t = RunWorkflowTool(workspace="/tmp/x", sessions=object(), app_config=object(), bus=None)
    # Must not raise even though there is no bus.
    await t._inject_result("x", name="qa",
                           inject_target={"channel": "websocket", "chat_id": "chatA", "session_key": None})


def test_terminal_progress_payload_marks_workflow_done():
    """The engine only emits per-node done=False frames; a terminal frame must be
    built after the run so the WORK panel (TUI + webui) can mark the workflow
    finished instead of leaving it stuck on 'running'."""
    from types import SimpleNamespace

    from durin.agent.tools.run_workflow import _terminal_progress_payload

    # Empty `nodes` map -> label falls back to node_id (skips node_label).
    workflow = SimpleNamespace(nodes={})
    runs = [
        SimpleNamespace(node_id="scan", status="passed", route_label="pass", iteration=1, budget=None),
        SimpleNamespace(node_id="fix", status="node_failed", route_label=None, iteration=1, budget=None),
    ]
    payload = _terminal_progress_payload(workflow, "run-1", runs)

    assert payload["done"] is True
    assert payload["run_id"] == "run-1"
    statuses = {n["id"]: n["status"] for n in payload["nodes"]}
    assert statuses == {"scan": "done", "fix": "failed"}
