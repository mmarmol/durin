"""stop() must swallow the CancelledError from the cancelled server task.

CancelledError is a BaseException (not Exception), so `except Exception` did not
catch it — it escaped and surfaced as an "uncaught exception" ERROR (with a scary
traceback) on every gateway stop.
"""
import asyncio

from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel


async def test_stop_swallows_cancelled_server_task():
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, MessageBus())
    channel._running = True

    # A task that raises CancelledError when awaited — i.e. the server task being
    # cancelled during shutdown.
    task = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.sleep(0)  # let it start
    task.cancel()
    channel._server_task = task

    # Must complete cleanly — no CancelledError propagating out of stop().
    await channel.stop()

    assert channel._server_task is None
    assert channel._running is False
