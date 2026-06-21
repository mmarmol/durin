"""Cross-process turn-lease test: two processes, one session, no clobbered turns."""
import asyncio
import multiprocessing as mp
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.session.manager import SessionManager
from durin.session.turn_lease import session_turn_lease


def _turn(workspace: str, key: str, role: str, text: str) -> None:
    async def run() -> None:
        sm = SessionManager(Path(workspace))
        path = sm._get_session_path(key)
        async with session_turn_lease(path, timeout=20.0):
            s = sm.reload(key)  # load-per-turn
            s.add_message(role, text)
            await asyncio.sleep(0.05)  # widen the race window
            sm.save(s)

    asyncio.run(run())


def test_two_processes_one_session_no_lost_turns(tmp_path: Path) -> None:
    key = "websocket:dup"
    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=_turn, args=(str(tmp_path), key, "user", "from-A"))
    p2 = ctx.Process(target=_turn, args=(str(tmp_path), key, "user", "from-B"))
    p1.start()
    p2.start()
    p1.join(30)
    p2.join(30)
    assert p1.exitcode == 0 and p2.exitcode == 0
    sm = SessionManager(tmp_path)
    msgs = sm.reload(key).messages
    texts = {m["content"] for m in msgs}
    assert texts == {"from-A", "from-B"}  # neither turn clobbered the other
    assert len(msgs) == 2


@pytest.mark.asyncio
async def test_dispatch_publishes_busy_message_on_lease_timeout(tmp_path: Path) -> None:
    """When the turn-lease cannot be acquired within the timeout, _dispatch must
    publish a clear 'session busy' outbound message instead of dropping the turn."""
    from durin.agent.loop import AgentLoop
    from durin.bus.events import InboundMessage
    from durin.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager") as MockSM, \
         patch("durin.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        MockSM.return_value._get_session_path.return_value = tmp_path / "test_session.jsonl"
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)

    @asynccontextmanager
    async def _busy_lease(path: Path, **kwargs) -> AsyncIterator[None]:
        raise TimeoutError("lease held by another process")
        yield  # pragma: no cover

    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="hello")

    with patch("durin.agent.loop.session_turn_lease", _busy_lease):
        await loop._dispatch(msg)

    out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    assert "busy" in out.content.lower() or "another window" in out.content.lower(), (
        f"Expected a 'session busy' message, got: {out.content!r}"
    )
