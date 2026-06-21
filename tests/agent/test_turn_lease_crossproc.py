"""Cross-process turn-lease test: two processes, one session, no clobbered turns."""
import asyncio
import multiprocessing as mp
from pathlib import Path

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
