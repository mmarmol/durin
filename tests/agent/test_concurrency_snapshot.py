from durin.agent.concurrency_snapshot import build_snapshot
from durin.utils.resizable_semaphore import ResizableSemaphore


def test_build_snapshot_shape_and_queued():
    interactive = ResizableSemaphore(4, name="interactive")
    ceiling = ResizableSemaphore(12, name="ceiling")
    snap = build_snapshot(
        interactive=interactive,
        ceiling=ceiling,
        subagent_running=2,
        subagent_limit=3,
        turn_sessions=["websocket:a", "websocket:b"],
        running_subagents=[("1a2b3c4d", "websocket:a", "research foo")],
    )
    assert snap["lanes"]["interactive"] == {"active": 0, "limit": 4, "waiting": 0}
    assert snap["lanes"]["ceiling"] == {"active": 0, "limit": 12, "waiting": 0}
    assert snap["lanes"]["subagents"] == {"active": 2, "limit": 3}
    assert snap["queued"] == 0
    kinds = [(w["kind"], w["id"]) for w in snap["work"]]
    assert ("turn", "turn:websocket:a") in kinds
    assert ("turn", "turn:websocket:b") in kinds
    assert ("subagent", "subagent:1a2b3c4d") in kinds
    assert all(w["status"] == "running" for w in snap["work"])


def test_queued_sums_interactive_and_ceiling_waiting():
    interactive = ResizableSemaphore(1, name="interactive")
    ceiling = ResizableSemaphore(1, name="ceiling")
    interactive._waiting = 2  # simulate two turns blocked on the lane
    ceiling._waiting = 1      # one subagent blocked on the ceiling
    snap = build_snapshot(
        interactive=interactive, ceiling=ceiling,
        subagent_running=0, subagent_limit=3,
        turn_sessions=[], running_subagents=[],
    )
    assert snap["queued"] == 3
