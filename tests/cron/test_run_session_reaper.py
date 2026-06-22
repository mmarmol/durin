"""Reaper for per-run cron sessions (cron:{id}:run:{ms})."""

from __future__ import annotations

from durin.cron.reaper import select_expired_run_sessions


def test_selects_only_old_run_sessions() -> None:
    now_ms = 100_000_000_000
    hour_ms = 3_600_000
    sessions = [
        # Old per-run cron session — should be reaped.
        {"key": f"cron:abc123:run:{now_ms - 50 * hour_ms}"},
        # Recent per-run cron session — within retention, kept.
        {"key": f"cron:abc123:run:{now_ms - 1 * hour_ms}"},
        # A regular (non-run) cron session — never reaped here.
        {"key": "cron:abc123"},
        # A normal user session — never reaped.
        {"key": "whatsapp:+1555"},
        # Another old run session for a different job — reaped.
        {"key": f"cron:def456:run:{now_ms - 200 * hour_ms}"},
    ]
    expired = select_expired_run_sessions(
        sessions, retention_hours=48, now_ms=now_ms
    )
    assert expired == [
        f"cron:abc123:run:{now_ms - 50 * hour_ms}",
        f"cron:def456:run:{now_ms - 200 * hour_ms}",
    ]


def test_retention_zero_reaps_nothing() -> None:
    now_ms = 100_000_000_000
    sessions = [
        {"key": f"cron:abc:run:{now_ms - 1_000_000_000}"},
        {"key": f"cron:abc:run:{now_ms}"},
    ]
    assert select_expired_run_sessions(sessions, retention_hours=0, now_ms=now_ms) == []


def test_malformed_run_suffix_is_ignored() -> None:
    now_ms = 100_000_000_000
    sessions = [
        {"key": "cron:abc:run:not-a-number"},
        {"key": "cron:abc:run:"},
    ]
    assert select_expired_run_sessions(sessions, retention_hours=48, now_ms=now_ms) == []


def test_boundary_at_retention_edge_is_kept() -> None:
    now_ms = 100_000_000_000
    retention_ms = 48 * 3_600_000
    sessions = [
        # Exactly at the edge — not strictly older, kept.
        {"key": f"cron:abc:run:{now_ms - retention_ms}"},
        # One ms past the edge — reaped.
        {"key": f"cron:abc:run:{now_ms - retention_ms - 1}"},
    ]
    expired = select_expired_run_sessions(
        sessions, retention_hours=48, now_ms=now_ms
    )
    assert expired == [f"cron:abc:run:{now_ms - retention_ms - 1}"]
