"""Turn-lease serialization for process_direct.

process_direct bypasses _dispatch and therefore historically skipped both
the in-process asyncio.Lock and the cross-process turn lease.  This test
pins the contract: process_direct MUST acquire session_turn_lease before
calling _process_message, mirror _dispatch's reload-before-process
semantics, and on TimeoutError log a warning and return None (never raise
to its caller, which is a background cron/heartbeat path).

See docs/architecture/concurrency.md.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.loop import AgentLoop, _SESSION_BUSY_NOTICE
from durin.bus.queue import MessageBus
from durin.session.manager import SessionManager
from durin.session.turn_lease import session_turn_lease


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


# ---------------------------------------------------------------------------
# Test 1: lease held by another holder -> process_direct skips (no clobber)
# ---------------------------------------------------------------------------


async def test_process_direct_skips_on_lease_timeout(tmp_path: Path) -> None:
    """With the turn lease held, process_direct must skip with a log warning.

    It must NOT raise TimeoutError to its caller (a cron/heartbeat path),
    and must NOT call _process_message (no clobber).
    """
    loop = _make_loop(tmp_path)

    process_message_calls: list = []

    async def fake_process_message(*args, **kwargs):
        process_message_calls.append(1)
        return None

    loop._process_message = fake_process_message  # type: ignore[method-assign]
    loop._connect_mcp = AsyncMock()

    @asynccontextmanager
    async def _busy_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        raise TimeoutError("held by another process")
        yield  # pragma: no cover

    with patch("durin.agent.loop.session_turn_lease", _busy_lease):
        result = await loop.process_direct("hello", session_key="cron:test")

    assert result is not None, "process_direct must return a response (not None) on timeout"
    assert result.content == _SESSION_BUSY_NOTICE, (
        f"process_direct must return the busy notice on timeout; got {result!r}"
    )
    assert process_message_calls == [], "_process_message must NOT be called on lease timeout"


# ---------------------------------------------------------------------------
# Test 2: no contention -> process_direct reloads and calls _process_message
# ---------------------------------------------------------------------------


async def test_process_direct_acquires_lease_and_reloads(tmp_path: Path) -> None:
    """Happy path: no contention, process_direct acquires lease + reloads session."""
    loop = _make_loop(tmp_path)

    reload_calls: list[str] = []
    original_reload = loop.sessions.reload

    def spy_reload(key: str):
        reload_calls.append(key)
        return original_reload(key)

    loop.sessions.reload = spy_reload  # type: ignore[method-assign]

    lease_entered = False
    original_lease = session_turn_lease

    @asynccontextmanager
    async def spy_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        nonlocal lease_entered
        lease_entered = True
        async with original_lease(path, **kwargs):
            yield

    process_message_calls: list = []

    async def fake_process_message(msg, session_key=None, **kwargs):
        process_message_calls.append(session_key or msg.session_key)
        return None

    loop._process_message = fake_process_message  # type: ignore[method-assign]
    loop._connect_mcp = AsyncMock()

    with patch("durin.agent.loop.session_turn_lease", spy_lease):
        await loop.process_direct("hello", session_key="cron:test")

    assert lease_entered, "session_turn_lease was not acquired"
    assert "cron:test" in reload_calls, "session must be reloaded under the lease"
    assert process_message_calls, "_process_message must have been called"


# ---------------------------------------------------------------------------
# Test 3: in-process asyncio.Lock prevents concurrent process_direct calls
#          on the same session key
# ---------------------------------------------------------------------------


async def test_process_direct_in_process_lock_serializes_same_session(tmp_path: Path) -> None:
    """Two concurrent process_direct calls on the same session must be serialized.

    process_direct acquires the per-session asyncio.Lock (self._session_locks)
    before acquiring the turn lease.  A second concurrent call must wait until
    the first finishes.  The session written by the first turn must not be lost.
    """
    key = "cron:overlap"
    sm = SessionManager(tmp_path)

    loop = _make_loop(tmp_path)
    loop._connect_mcp = AsyncMock()

    # Pre-seed the session so reload finds something.
    s = sm.get_or_create(key)
    sm.save(s)

    order: list[str] = []
    first_started = asyncio.Event()
    first_may_finish = asyncio.Event()

    async def fake_process_message(msg, session_key=None, **kwargs):
        tag = msg.content
        order.append(f"start:{tag}")
        if tag == "first":
            first_started.set()
            await first_may_finish.wait()
        order.append(f"end:{tag}")
        return None

    loop._process_message = fake_process_message  # type: ignore[method-assign]

    # Patch the lease to be a no-op (we're testing the asyncio.Lock layer here).
    @asynccontextmanager
    async def noop_lease(path, **kwargs) -> AsyncIterator[None]:
        yield

    with patch("durin.agent.loop.session_turn_lease", noop_lease):
        t1 = asyncio.create_task(loop.process_direct("first", session_key=key))
        await asyncio.wait_for(first_started.wait(), timeout=5.0)
        # Start the second call while first is in progress.
        t2 = asyncio.create_task(loop.process_direct("second", session_key=key))
        # Let the first finish.
        first_may_finish.set()
        await asyncio.gather(t1, t2)

    # The asyncio.Lock must ensure "first" fully completes before "second" starts.
    assert order.index("end:first") < order.index("start:second"), (
        f"second started before first ended: {order}"
    )
