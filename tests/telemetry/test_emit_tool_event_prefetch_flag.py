"""The automatic per-turn memory prefetch (`AgentLoop._memory_prefetch`) and
the model's own `memory_search` tool call both end up emitting the same
`memory.recall*` event types through `emit_tool_event`. A `prefetch`
ContextVar, bound around the prefetch's search and read by
`emit_tool_event`, is what tells the resulting rows apart.

The tricky case: `memory_search` runs its pipeline via `asyncio.to_thread`,
which executes in a COPY of the calling context. A search that blows past
`memory.prefetch.timeout_s` is abandoned by `asyncio.wait_for`, but the
thread it started keeps running and can still emit `memory.recall*` rows
after the parent has already reset the flag — those rows must still carry
`prefetch: true`, since the thread's copied context was made before the
reset.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path


def test_prefetch_flag_marks_only_recall_events_while_bound(tmp_path: Path) -> None:
    from durin.agent.tools._telemetry import emit_tool_event
    from durin.telemetry.logger import (
        bind_prefetch_search,
        bind_telemetry,
        get_session_logger,
        reset_prefetch_search,
        reset_telemetry,
    )

    logger = get_session_logger("telegram:c1", base_dir=tmp_path)
    tel_token = bind_telemetry(logger)
    try:
        pf_token = bind_prefetch_search()
        try:
            emit_tool_event("memory.recall.lexical", {
                "route": "unicode61", "query_chars": 3, "cjk_chars": 0,
                "hit_count": 1, "duration_ms": 1.0,
            })
        finally:
            reset_prefetch_search(pf_token)

        # Outside the bind, the same event type gets no `prefetch` key at all
        # — not `prefetch: false` — so a dashboard can tell "known not to be
        # the prefetch's" apart from "recorded before this flag existed".
        emit_tool_event("memory.recall.lexical", {
            "route": "unicode61", "query_chars": 3, "cjk_chars": 0,
            "hit_count": 1, "duration_ms": 1.0,
        })

        # A non-recall event never gets the key, even while bound.
        pf_token = bind_prefetch_search()
        try:
            emit_tool_event("memory.upsert_entity", {
                "ref": "topic:x", "committed": True, "retries": 0,
            })
        finally:
            reset_prefetch_search(pf_token)
    finally:
        reset_telemetry(tel_token)

    lines = [
        json.loads(line)
        for line in logger.path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(lines) == 3
    assert lines[0]["type"] == "memory.recall.lexical"
    assert lines[0]["data"]["prefetch"] is True
    assert lines[1]["type"] == "memory.recall.lexical"
    assert "prefetch" not in lines[1]["data"]
    assert lines[2]["type"] == "memory.upsert_entity"
    assert "prefetch" not in lines[2]["data"]


def test_prefetch_flag_survives_into_an_abandoned_thread_copy(tmp_path: Path) -> None:
    """`asyncio.to_thread` copies the calling context when the thread
    starts. Binding the flag, kicking off the thread, and resetting the
    flag right after (mirroring `_memory_prefetch`'s bind/`wait_for`/reset)
    must not stop the already-running thread's copy from still carrying
    it — it keeps emitting under the old value for as long as it runs,
    exactly like the abandoned search thread a prefetch timeout leaves
    behind."""
    from durin.agent.tools._telemetry import emit_tool_event
    from durin.telemetry.logger import (
        bind_prefetch_search,
        bind_telemetry,
        get_session_logger,
        reset_prefetch_search,
        reset_telemetry,
    )

    logger = get_session_logger("telegram:c1", base_dir=tmp_path)

    def _slow_emit() -> None:
        time.sleep(0.15)  # outlives the parent's reset below
        emit_tool_event("memory.recall.rrf", {
            "vector_count": 1, "lexical_count": 0, "grep_count": 0,
            "fused_count": 1, "boosted": False, "duration_ms": 1.0,
        })

    async def _run() -> None:
        tel_token = bind_telemetry(logger)
        pf_token = bind_prefetch_search()
        task = asyncio.ensure_future(asyncio.to_thread(_slow_emit))
        await asyncio.sleep(0)  # let the task start so the thread's context copy is made
        reset_telemetry(tel_token)
        reset_prefetch_search(pf_token)
        await task  # the abandoned thread finishes on its own time

    asyncio.run(_run())

    lines = [
        json.loads(line)
        for line in logger.path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(lines) == 1
    assert lines[0]["type"] == "memory.recall.rrf"
    assert lines[0]["data"]["prefetch"] is True
