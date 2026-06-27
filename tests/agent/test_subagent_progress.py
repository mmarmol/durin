import asyncio
import pytest
from durin.agent.subagent import _SubagentHook, SubagentStatus
from durin.agent.hook import AgentHookContext  # real import path (not durin.agent.hooks)


class _Bus:
    def __init__(self):
        self.sent = []

    async def publish_outbound(self, msg):
        self.sent.append(msg)


def _ctx(iteration, tool_events):
    # Build a minimal context object with the fields after_iteration reads.
    class C:
        pass
    c = C()
    c.iteration = iteration
    c.tool_events = tool_events
    c.usage = {}
    c.error = None
    return c


@pytest.mark.asyncio
async def test_hook_emits_running_progress_frame_per_iteration():
    bus = _Bus()
    status = SubagentStatus(task_id="t1", label="research", task_description="do x", started_at=0.0)
    hook = _SubagentHook(
        task_id="t1", status=status, bus=bus,
        origin={"channel": "websocket", "chat_id": "websocket:chatA", "session_key": "websocket:chatA"},
    )
    await hook.after_iteration(_ctx(2, [{"name": "grep", "status": "ok"}]))
    assert len(bus.sent) == 1
    ev = bus.sent[0].metadata["_tool_events"][0]
    assert ev["call_id"] == "subagent:t1"
    assert ev["name"] == "subagent_result"
    assert ev["phase"] == "running"
    assert ev["progress"]["iteration"] == 2
    assert bus.sent[0].chat_id == "websocket:chatA"


@pytest.mark.asyncio
async def test_hook_no_emit_without_bus():
    """No bus → after_iteration still runs without error, just no emit."""
    status = SubagentStatus(task_id="t2", label="x", task_description="y", started_at=0.0)
    hook = _SubagentHook(task_id="t2", status=status)
    # Should not raise even without bus/origin
    await hook.after_iteration(_ctx(1, []))


@pytest.mark.asyncio
async def test_hook_no_emit_without_chat_id():
    """Bus present but no chat_id → no emit (best-effort guard)."""
    bus = _Bus()
    status = SubagentStatus(task_id="t3", label="x", task_description="y", started_at=0.0)
    hook = _SubagentHook(task_id="t3", status=status, bus=bus, origin={"channel": "websocket"})
    await hook.after_iteration(_ctx(1, []))
    assert len(bus.sent) == 0
