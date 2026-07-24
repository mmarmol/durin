"""Gateway malloc janitor: trim glibc arenas only when they retain enough
freed-but-unreturned memory, and report what the trim recovered."""
from __future__ import annotations

import durin.service.wiring as wiring


def _snapshot(free_mb: float, rss_mb: float = 3000.0) -> dict:
    return {
        "rss_mb": rss_mb,
        "malloc_system_mb": 3500.0,
        "malloc_in_use_mb": 3500.0 - free_mb,
        "malloc_free_mb": free_mb,
    }


def test_no_trim_below_threshold(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "durin.utils.glibc_malloc.malloc_trim", lambda: calls.append(1) or True)
    assert wiring._maybe_trim_malloc(_snapshot(free_mb=100.0)) is None
    assert calls == []


def test_no_trim_without_glibc_signal(monkeypatch) -> None:
    monkeypatch.setattr(
        "durin.utils.glibc_malloc.malloc_trim",
        lambda: (_ for _ in ()).throw(AssertionError("must not trim")))
    snapshot = {"rss_mb": 3000.0, "malloc_system_mb": 0.0,
                "malloc_in_use_mb": 0.0, "malloc_free_mb": 0.0}
    assert wiring._maybe_trim_malloc(snapshot) is None


def test_trim_above_threshold_reports_recovery(monkeypatch) -> None:
    monkeypatch.setattr("durin.utils.glibc_malloc.malloc_trim", lambda: True)
    monkeypatch.setattr(
        "durin.utils.process_tree.process_rss_mb", lambda: 900.0)
    event = wiring._maybe_trim_malloc(_snapshot(free_mb=1500.0, rss_mb=3600.0))
    assert event == {
        "rss_before_mb": 3600.0,
        "rss_after_mb": 900.0,
        "retained_mb": 1500.0,
        "released": True,
    }


def test_trim_event_is_catalogued() -> None:
    from durin.telemetry.schema import EVENTS

    assert "gateway.memory.trimmed" in EVENTS
