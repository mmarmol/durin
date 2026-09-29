"""Gateway malloc janitor: trim glibc arenas when the freed memory they have
piled up since the last trim is large next to the live heap, report what the
trim recovered, and trim at once when asked (after a voice engine unload)."""
from __future__ import annotations

import threading

import pytest
from loguru import logger

import durin.service.wiring as wiring


def _snapshot(free_mb: float, in_use_mb: float = 170.0, rss_mb: float = 900.0) -> dict:
    return {
        "rss_mb": rss_mb,
        "malloc_system_mb": in_use_mb + free_mb,
        "malloc_in_use_mb": in_use_mb,
        "malloc_free_mb": free_mb,
    }


@pytest.fixture
def glibc(monkeypatch):
    """A fake glibc whose mallinfo2 keeps reporting trimmed pages as free,
    as the real one does: malloc_trim madvises page runs of free chunks away
    but leaves the chunks (and so the free figure) in the arena's books."""
    state = {"free_mb": 0.0, "trims": 0}

    def _trim() -> bool:
        state["trims"] += 1
        return True

    monkeypatch.setattr("durin.utils.glibc_malloc.malloc_trim", _trim)
    monkeypatch.setattr(
        "durin.utils.glibc_malloc.malloc_stats_mb",
        lambda: {"system_mb": 0.0, "in_use_mb": 0.0, "free_mb": state["free_mb"]},
    )
    monkeypatch.setattr("durin.utils.process_tree.process_rss_mb", lambda: 700.0)
    return state


def _pass(janitor, glibc, free_mb: float, **kw):
    glibc["free_mb"] = free_mb
    return janitor.maybe_trim(_snapshot(free_mb, **kw))


def test_no_trim_without_glibc_signal(monkeypatch) -> None:
    monkeypatch.setattr(
        "durin.utils.glibc_malloc.malloc_trim",
        lambda: (_ for _ in ()).throw(AssertionError("must not trim")))
    snapshot = {"rss_mb": 3000.0, "malloc_system_mb": 0.0,
                "malloc_in_use_mb": 0.0, "malloc_free_mb": 0.0}
    assert wiring._MallocJanitor().maybe_trim(snapshot) is None
    assert wiring._MallocJanitor().maybe_trim(snapshot, force=True) is None


def test_trims_steady_retention_that_rivals_the_live_heap(glibc) -> None:
    # The box shape: ~240MB freed-but-held against ~170MB in use for a day.
    # Far below any fixed half-gigabyte bar, yet more than half the heap.
    assert _pass(wiring._MallocJanitor(), glibc, 240.0, in_use_mb=170.0) is not None
    assert glibc["trims"] == 1


def test_small_retention_is_left_alone(glibc) -> None:
    janitor = wiring._MallocJanitor()
    # Under half of a large live heap: normal allocator slack.
    assert _pass(janitor, glibc, 400.0, in_use_mb=2000.0) is None
    # A tiny heap never trims for a few MB, whatever the ratio.
    assert _pass(janitor, glibc, 40.0, in_use_mb=30.0) is None
    assert glibc["trims"] == 0


def test_no_retrim_after_a_spike_until_new_memory_is_freed(glibc) -> None:
    janitor = wiring._MallocJanitor()
    assert _pass(janitor, glibc, 1500.0) is not None
    # mallinfo2 still reports the trimmed pages as free; the few MB freed
    # since are not worth another trim every pass.
    assert _pass(janitor, glibc, 1510.0) is None
    assert _pass(janitor, glibc, 1532.0) is None
    assert glibc["trims"] == 1
    # The heap reuses the trimmed chunks (paging them back in), then frees
    # them again: that is fresh resident retention, and it is trimmed.
    assert _pass(janitor, glibc, 1000.0) is None
    assert _pass(janitor, glibc, 1400.0) is not None
    assert glibc["trims"] == 2


def test_forced_trim_ignores_the_bar(glibc) -> None:
    assert _pass(wiring._MallocJanitor(), glibc, 10.0) is None
    glibc["free_mb"] = 10.0
    assert wiring._MallocJanitor().maybe_trim(_snapshot(10.0), force=True) is not None
    assert glibc["trims"] == 1


def test_trim_event_reports_recovery(glibc) -> None:
    event = _pass(wiring._MallocJanitor(), glibc, 1500.0, rss_mb=3600.0)
    assert event == {
        "rss_before_mb": 3600.0,
        "rss_after_mb": 700.0,
        "retained_mb": 1500.0,
        "grown_mb": 1500.0,
        "forced": False,
        "released": True,
    }


def _capture_logs() -> tuple[list[tuple[str, str]], int]:
    records: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: records.append((m.record["level"].name, m.record["message"])),
        level="DEBUG", format="{message}")
    return records, sink


def test_pass_logs_only_a_meaningful_release_at_info(glibc, monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.agent.tools._telemetry.emit_tool_event",
        lambda kind, data: events.append((kind, data)))
    rss_after = {"mb": 300.0}
    monkeypatch.setattr(
        "durin.utils.process_tree.process_rss_mb", lambda: rss_after["mb"])
    snapshots = iter([_snapshot(1500.0, rss_mb=1400.0), _snapshot(10.0, rss_mb=310.0)])
    monkeypatch.setattr(
        "durin.utils.process_tree.memory_snapshot", lambda: next(snapshots))
    janitor = wiring._MallocJanitor()
    records, sink = _capture_logs()
    try:
        glibc["free_mb"] = 1500.0
        wiring._janitor_pass(janitor, force=False)      # gives back 1.1GB
        rss_after["mb"] = 302.0
        glibc["free_mb"] = 10.0
        wiring._janitor_pass(janitor, force=True)       # gives back 8MB
    finally:
        logger.remove(sink)

    trims = [(level, msg) for level, msg in records if "malloc janitor" in msg]
    assert [level for level, _ in trims] == ["INFO", "DEBUG"]
    kinds = [kind for kind, _ in events]
    assert kinds == ["gateway.memory", "gateway.memory.trimmed",
                     "gateway.memory", "gateway.memory.trimmed"]


class _StopLoop(BaseException):
    """Ends the janitor loop from inside a pass (it survives any Exception)."""


def test_request_wakes_the_janitor_for_a_forced_pass(monkeypatch) -> None:
    passes: list[bool] = []
    first_pass = threading.Event()

    def _fake_pass(_janitor, *, force: bool) -> None:
        passes.append(force)
        first_pass.set()
        if len(passes) == 2:
            raise _StopLoop

    monkeypatch.setattr(wiring, "_janitor_pass", _fake_pass)
    monkeypatch.setattr(wiring, "_malloc_trim_requested", threading.Event())

    def _run() -> None:
        try:
            wiring._janitor_loop(period_s=3600.0)
        except _StopLoop:
            pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert first_pass.wait(5.0)
    wiring.request_malloc_trim()
    thread.join(5.0)

    assert not thread.is_alive(), "the request did not wake the janitor"
    assert passes == [False, True]


def test_trim_event_is_catalogued() -> None:
    from durin.telemetry.schema import EVENTS

    assert "gateway.memory.trimmed" in EVENTS


def test_payloads_match_their_catalog_entries(glibc) -> None:
    from durin.telemetry.schema import EVENTS
    from durin.utils.process_tree import memory_snapshot

    trimmed = _pass(wiring._MallocJanitor(), glibc, 1500.0)
    assert set(trimmed) == set(EVENTS["gateway.memory.trimmed"].__annotations__)
    assert set(memory_snapshot()) == set(EVENTS["gateway.memory"].__annotations__)
