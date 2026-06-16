"""Post-compaction loop guard (OpenClaw-inspired Tier 2 C2).

After a successful compaction, the consolidator arms the guard with a
``window_size`` (default 3). The runner observes the next N tool calls.
When the SAME ``(tool_name, args_hash, result_hash)`` triple appears
``window_size`` times within the window, the guard trips —
consolidation didn't break the loop, so the turn aborts.

This is narrower than 1A (which only blocks repeated FAILED calls): C2
also catches the case where a tool keeps SUCCEEDING with identical
outputs but the model can't act on them.
"""

from __future__ import annotations

from durin.utils.post_compaction_guard import (
    Observation,
    PostCompactionLoopGuard,
    hash_args,
    hash_result,
)

# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def test_hash_args_is_stable_across_dict_order():
    """Different insertion orders for the same dict must produce the
    same hash — otherwise a guard would never trip on real loops."""
    a = {"foo": 1, "bar": 2}
    b = {"bar": 2, "foo": 1}
    assert hash_args(a) == hash_args(b)


def test_hash_args_differs_for_different_values():
    assert hash_args({"x": 1}) != hash_args({"x": 2})


def test_hash_args_handles_non_serialisable():
    """Sets / custom objects are coerced via repr — never raise."""
    class _Custom:
        def __repr__(self):
            return "<Custom>"
    h1 = hash_args(_Custom())
    h2 = hash_args(_Custom())
    assert h1 == h2  # repr is deterministic for this class


def test_hash_result_distinguishes_lists_and_strings():
    assert hash_result("foo") != hash_result(["foo"])


# ---------------------------------------------------------------------------
# Guard lifecycle
# ---------------------------------------------------------------------------


def _obs(name: str, args: dict, result: str) -> Observation:
    return Observation(
        tool_name=name,
        args_hash=hash_args(args),
        result_hash=hash_result(result),
    )


def test_unarmed_guard_returns_no_abort():
    """Without arming, every observation passes through unchanged."""
    g = PostCompactionLoopGuard(window_size=3)
    v = g.observe("s1", _obs("read_file", {"path": "a.py"}, "content"))
    assert v.should_abort is False
    assert v.armed_after is False


def test_armed_guard_trips_after_window_size_identical_triples():
    g = PostCompactionLoopGuard(window_size=3)
    g.arm("s1")

    obs = _obs("read_file", {"path": "a.py"}, "same content")
    v1 = g.observe("s1", obs)
    assert v1.should_abort is False
    assert v1.armed_after is True

    v2 = g.observe("s1", obs)
    assert v2.should_abort is False

    v3 = g.observe("s1", obs)
    assert v3.should_abort is True
    assert v3.tool_name == "read_file"
    assert v3.repeat_count == 3


def test_armed_guard_does_not_trip_when_args_differ():
    """Same tool name + different args → not a loop."""
    g = PostCompactionLoopGuard(window_size=3)
    g.arm("s1")

    g.observe("s1", _obs("read_file", {"path": "a.py"}, "c"))
    g.observe("s1", _obs("read_file", {"path": "b.py"}, "c"))
    v3 = g.observe("s1", _obs("read_file", {"path": "c.py"}, "c"))
    assert v3.should_abort is False


def test_armed_guard_does_not_trip_when_results_differ():
    """Same tool name + same args + DIFFERENT result → progress made,
    don't trip."""
    g = PostCompactionLoopGuard(window_size=3)
    g.arm("s1")

    g.observe("s1", _obs("read_file", {"path": "a.py"}, "v1"))
    g.observe("s1", _obs("read_file", {"path": "a.py"}, "v2"))
    v3 = g.observe("s1", _obs("read_file", {"path": "a.py"}, "v3"))
    assert v3.should_abort is False


def test_guard_auto_disarms_after_window_exhausted():
    """If the window passes without a trip, subsequent observations are
    no-ops until the next arm."""
    g = PostCompactionLoopGuard(window_size=3)
    g.arm("s1")

    g.observe("s1", _obs("a", {}, "1"))
    g.observe("s1", _obs("b", {}, "2"))
    g.observe("s1", _obs("c", {}, "3"))
    # Window exhausted.
    v = g.observe("s1", _obs("a", {}, "1"))
    assert v.should_abort is False
    assert v.armed_after is False
    assert v.remaining_attempts == 0


def test_per_session_isolation():
    """Arming one session must not affect another."""
    g = PostCompactionLoopGuard(window_size=3)
    g.arm("s1")
    # s2 not armed.
    obs = _obs("read", {}, "x")
    v = g.observe("s2", obs)
    assert v.should_abort is False
    assert v.armed_after is False


def test_arming_resets_history():
    """A fresh arm clears any prior partial window — otherwise a
    just-armed guard could trip immediately on stale state."""
    g = PostCompactionLoopGuard(window_size=3)
    g.arm("s1")
    g.observe("s1", _obs("x", {}, "1"))
    g.observe("s1", _obs("x", {}, "1"))
    # Re-arm.
    g.arm("s1")
    # Single new observation must NOT be a trip — counter restarts.
    v = g.observe("s1", _obs("x", {}, "1"))
    assert v.should_abort is False


def test_window_size_zero_disables_guard():
    """``window_size=0`` (e.g. env-set to 0) means the guard is disabled
    even after arming."""
    g = PostCompactionLoopGuard(window_size=0)
    g.arm("s1")
    obs = _obs("a", {}, "1")
    v = g.observe("s1", obs)
    assert v.should_abort is False
    assert v.armed_after is False


def test_env_override(monkeypatch):
    monkeypatch.setenv("DURIN_POST_COMPACTION_GUARD_WINDOW", "5")
    g = PostCompactionLoopGuard()
    assert g.window_size == 5


def test_env_override_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("DURIN_POST_COMPACTION_GUARD_WINDOW", "garbage")
    g = PostCompactionLoopGuard()
    assert g.window_size == 3


def test_reset_drops_state():
    g = PostCompactionLoopGuard(window_size=3)
    g.arm("s1")
    g.observe("s1", _obs("x", {}, "1"))
    g.reset("s1")
    assert g.is_armed("s1") is False
