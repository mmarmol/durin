"""memory_store auto-records the originating session+turn as provenance."""

from __future__ import annotations

import asyncio
from pathlib import Path

from durin.agent.tools.memory_store import MemoryStoreTool
from durin.memory.storage import load_entry
from durin.telemetry.logger import (
    bind_telemetry,
    get_session_logger,
    reset_telemetry,
)


def _run(tool: MemoryStoreTool, **kw) -> dict:
    return asyncio.run(tool.execute(**kw))


def test_records_session_turn_ref_inside_a_bound_turn(tmp_path: Path) -> None:
    tool = MemoryStoreTool(workspace=tmp_path)
    tlog = get_session_logger("websocket:abc123", base_dir=tmp_path)
    tlog.set_iteration(3)
    token = bind_telemetry(tlog)
    try:
        res = _run(
            tool, content="mxHERO is an email-to-cloud company",
            class_name="stable", entities=["company:mxhero"],
        )
    finally:
        reset_telemetry(token)

    # store_memory strips the [[ ]] wiki-brackets from source_refs; the
    # graph regex matches the bracket-less form either way.
    entry = load_entry(Path(res["path"]))
    assert "sessions/websocket_abc123.md#turn-3" in (entry.source_refs or [])


def test_no_session_ref_outside_a_turn(tmp_path: Path) -> None:
    """Internal/dream writes (no bound telemetry) add no session ref."""
    tool = MemoryStoreTool(workspace=tmp_path)
    res = _run(tool, content="x", class_name="stable", entities=["company:mxhero"])
    entry = load_entry(Path(res["path"]))
    assert not any("sessions/" in r for r in (entry.source_refs or []))


def test_session_ref_not_duplicated_if_already_present(tmp_path: Path) -> None:
    tool = MemoryStoreTool(workspace=tmp_path)
    tlog = get_session_logger("websocket:abc", base_dir=tmp_path)
    tlog.set_iteration(1)
    token = bind_telemetry(tlog)
    ref = "[[sessions/websocket_abc.md#turn-1]]"
    try:
        res = _run(tool, content="x", class_name="stable", source_refs=[ref])
    finally:
        reset_telemetry(token)
    entry = load_entry(Path(res["path"]))
    # Stored bracket-less; must appear exactly once (no double-append).
    assert (entry.source_refs or []).count("sessions/websocket_abc.md#turn-1") == 1
