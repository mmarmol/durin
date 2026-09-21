"""The archived-summary block tells the model how to get the archived turns
back.

After a compaction the model sees a summary in place of the turns it
replaced. The full text of those turns is still in the session archive and
``session_search`` reaches it, but nothing in the prompt said so: the only
hint was the tool's own description, and a tool description is a weak
signal. A one-line pointer inside the block names the tool and the query
shape, so a model that needs a value the summary dropped knows where to
look.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.memory.session_summary_store import write_session_summary


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


def test_own_compaction_summary_points_at_session_search(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:s1", "- decided to use X", last_active=date(2026, 9, 21))

    block = loop._format_pending_summary(loop.sessions.get_or_create("websocket:s1"))

    assert block is not None
    assert "=== END ARCHIVED SUMMARY ===" in block
    pointer = block.split("=== END ARCHIVED SUMMARY ===", 1)[1]
    assert "session_search(" in pointer
    assert "[archived" in pointer
    # The pointer is a footer, not part of the summary text.
    assert block.index("- decided to use X") < block.index("session_search(")


def test_previous_session_summary_carries_no_archive_pointer(tmp_path: Path) -> None:
    """Continuity shows another session's summary; the fresh session has no
    archive of its own yet, so a pointer at its own archive would mislead."""
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:old", "- decided to use X", last_active=date(2026, 9, 1))

    block = loop._format_pending_summary(loop.sessions.get_or_create("websocket:new"))

    assert block is not None
    assert "PREVIOUS SESSION SUMMARY" in block
    assert "session_search(" not in block
