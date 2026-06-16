"""Tests for the in-turn pending-answer registry (blocking ask_user)."""

from __future__ import annotations

import pytest

from durin.agent import pending_answers as pa


@pytest.fixture(autouse=True)
def _clean_registry():
    pa.reset()
    yield
    pa.reset()


@pytest.mark.asyncio
async def test_create_and_resolve_delivers_answer():
    fut = pa.create("websocket:1")
    assert pa.is_waiting("websocket:1")
    assert pa.resolve("websocket:1", "green") is True
    assert await fut == "green"
    # Consumed: a second resolve finds no waiter.
    assert pa.resolve("websocket:1", "again") is False
    assert not pa.is_waiting("websocket:1")


@pytest.mark.asyncio
async def test_resolve_without_waiter_returns_false():
    assert pa.resolve("websocket:none", "x") is False


@pytest.mark.asyncio
async def test_fallback_delivers_sentinel():
    fut = pa.create("cli:1")
    assert pa.fallback("cli:1") is True
    assert await fut is pa.FALLBACK
    assert not pa.is_waiting("cli:1")


@pytest.mark.asyncio
async def test_discard_clears_only_own_future():
    fut1 = pa.create("k")
    pa.discard("k", fut1)
    assert not pa.is_waiting("k")
    # Discarding a stale handle never removes a newer waiter.
    fut2 = pa.create("k")
    pa.discard("k", fut1)
    assert pa.is_waiting("k")
    pa.discard("k", fut2)
    assert not pa.is_waiting("k")
    fut1.cancel()
    fut2.cancel()


@pytest.mark.asyncio
async def test_create_replaces_stale_waiter():
    fut1 = pa.create("k")
    fut2 = pa.create("k")
    assert fut1.cancelled()
    assert pa.resolve("k", "answer") is True
    assert await fut2 == "answer"


@pytest.mark.asyncio
async def test_resolve_on_done_future_is_safe():
    fut = pa.create("k")
    fut.cancel()
    # Registry entry still points at the cancelled future — resolve must
    # not raise and must report no live waiter.
    assert pa.resolve("k", "x") is False
