"""RequestContextVar: one tool instance, each concurrent turn sees its own context."""
from __future__ import annotations

import asyncio

import pytest

from durin.agent.tools.context import RequestContext, RequestContextVar


@pytest.mark.asyncio
async def test_each_task_sees_the_context_it_set():
    holder = RequestContextVar()
    a = RequestContext(channel="websocket", chat_id="A", session_key="websocket:A")
    b = RequestContext(channel="telegram", chat_id="B", session_key="telegram:B")

    async def turn(ctx, delay):
        holder.set(ctx)
        await asyncio.sleep(delay)  # the other task sets its own value meanwhile
        return holder.get()

    got_a, got_b = await asyncio.gather(turn(a, 0.05), turn(b, 0.0))
    assert got_a is a and got_b is b


def test_unset_is_none():
    assert RequestContextVar().get() is None


def test_two_holders_do_not_share_a_value():
    one, two = RequestContextVar(), RequestContextVar()
    one.set(RequestContext(channel="cli", chat_id="x", session_key="cli:x"))
    assert two.get() is None
