import asyncio

import pytest

from durin.agent import pending_answers as pa


@pytest.fixture(autouse=True)
def _reset():
    pa.reset()
    yield
    pa.reset()


@pytest.mark.asyncio
async def test_default_waiter_is_a_question():
    pa.create("websocket:s")
    assert pa.waiting_kind("websocket:s") == "question"
    assert pa.waiting_ref("websocket:s") is None


@pytest.mark.asyncio
async def test_approval_waiter_carries_its_record_id():
    fut = pa.create("websocket:s", kind="approval", ref="abc123abc123")
    assert pa.waiting_kind("websocket:s") == "approval"
    assert pa.waiting_ref("websocket:s") == "abc123abc123"
    assert pa.resolve("websocket:s", "approve") is True
    assert await fut == "approve"
    assert pa.waiting_kind("websocket:s") is None
