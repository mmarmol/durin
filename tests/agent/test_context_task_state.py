"""build_messages emits the task-state anchor block."""
from __future__ import annotations

from durin.agent.context import ContextBuilder
from durin.session.decision_log import add_decision


def test_build_messages_includes_task_state_block(tmp_path):
    meta: dict = {}
    add_decision(meta, "decision survives compaction", source="auto", ts="t1")
    builder = ContextBuilder(workspace=tmp_path)
    messages = builder.build_messages(
        history=[],
        current_message="hello",
        session_metadata=meta,
    )
    blob = "\n".join(
        m["content"] if isinstance(m.get("content"), str) else str(m.get("content"))
        for m in messages
    )
    assert "<task-state>" in blob
    assert "## Decisions & findings" in blob
    assert "decision survives compaction" in blob
