"""A test that ends while a turn still waits on a person must not leave that
wait to the next test: the suite clears the waiter registry and the approval
hand-off map around every test. The two tests below run in file order; the
first leaks on purpose, as a test that fails mid-wait would."""
from __future__ import annotations

from durin.agent import approval
from durin.agent import pending_answers as pa


async def test_a_test_that_ends_mid_wait_leaves_a_waiter_behind():
    pa.create("websocket:leak", kind="approval", ref="r-leak")
    approval._HANDOFF_DECIDED_BY["r-leak"] = {"kind": "user", "channel": "websocket"}
    assert pa.is_waiting("websocket:leak")


def test_the_next_test_starts_with_no_one_waiting():
    assert not pa.is_waiting("websocket:leak")
    assert pa.waiting_kind("websocket:leak") is None
    assert approval._HANDOFF_DECIDED_BY == {}
