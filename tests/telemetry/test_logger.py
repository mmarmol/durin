"""Tests for structured telemetry logger."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from durin.telemetry.logger import TelemetryLogger, get_session_logger


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "test.jsonl"


class TestTelemetryLogger:
    def test_creates_file_on_first_log(self, log_path: Path):
        tl = TelemetryLogger(log_path)
        tl.log("test.event", {"key": "value"})
        assert log_path.exists()

    def test_appends_jsonl(self, log_path: Path):
        tl = TelemetryLogger(log_path)
        tl.log("event.a", {"x": 1})
        tl.log("event.b", {"y": 2})

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["type"] == "event.a"
        assert first["data"]["x"] == 1
        assert "ts" in first

    def test_has_timestamp(self, log_path: Path):
        tl = TelemetryLogger(log_path)
        tl.log("test", {"a": 1})

        entry = json.loads(log_path.read_text().strip())
        assert isinstance(entry["ts"], float)
        assert entry["ts"] > 1_700_000_000

    def test_log_without_data(self, log_path: Path):
        tl = TelemetryLogger(log_path)
        tl.log("bare.event")

        entry = json.loads(log_path.read_text().strip())
        assert entry["type"] == "bare.event"
        assert "data" not in entry

    def test_respects_max_events(self, tmp_path: Path):
        path = tmp_path / "overflow.jsonl"
        tl = TelemetryLogger(path)
        # Patch max for testing
        tl._count = 9_999
        tl.log("last")
        tl.log("over_limit")

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["type"] == "last"


class TestGetSessionLogger:
    def test_creates_logger_with_date_suffix(self, tmp_path: Path):
        tl = get_session_logger("websocket:abc-123", base_dir=tmp_path)
        tl.log("test")
        assert tl.path.parent == tmp_path
        assert "websocket_abc-123" in tl.path.name
        assert ".jsonl" in tl.path.name

    def test_sanitizes_special_characters(self, tmp_path: Path):
        tl = get_session_logger("ws://evil:path/../../etc", base_dir=tmp_path)
        assert "/" not in tl.path.name
        assert ".." not in tl.path.name

    def test_creates_parent_dirs(self, tmp_path: Path):
        nested = tmp_path / "deep" / "nested"
        tl = get_session_logger("test", base_dir=nested)
        tl.log("hello")
        assert tl.path.exists()


def _types(path: Path) -> list[str]:
    return [json.loads(line)["type"] for line in path.read_text().splitlines()]


class TestDayRollover:
    """A logger that outlives midnight (the gateway's is bound once at
    startup) must file each event under the local date the event happened,
    so a reader looking at a day's file finds that day's events."""

    @pytest.fixture
    def clock(self, monkeypatch):
        from durin.telemetry import logger as tlog

        now = [datetime(2026, 9, 27, 23, 59, 50).timestamp()]
        monkeypatch.setattr(tlog, "time", SimpleNamespace(time=lambda: now[0]))
        tlog.close_all_handles()
        yield now
        tlog.close_all_handles()

    def test_events_after_midnight_go_to_the_new_days_file(self, tmp_path: Path, clock):
        tl = get_session_logger("gateway", base_dir=tmp_path)
        tl.log("before_midnight")
        clock[0] = datetime(2026, 9, 28, 0, 0, 10).timestamp()
        tl.log("after_midnight")
        clock[0] = datetime(2026, 9, 29, 7, 0, 0).timestamp()
        tl.log("two_days_later")

        assert _types(tmp_path / "gateway_2026-09-27.jsonl") == ["before_midnight"]
        assert _types(tmp_path / "gateway_2026-09-28.jsonl") == ["after_midnight"]
        assert _types(tmp_path / "gateway_2026-09-29.jsonl") == ["two_days_later"]
        assert tl.path == tmp_path / "gateway_2026-09-29.jsonl"

    def test_event_cap_restarts_with_each_days_file(self, tmp_path: Path, clock, monkeypatch):
        from durin.telemetry import logger as tlog

        monkeypatch.setattr(tlog, "_MAX_EVENTS_PER_FILE", 2)
        tl = get_session_logger("cron_dream", base_dir=tmp_path)
        for name in ("a", "b", "over_cap"):
            tl.log(name)
        clock[0] = datetime(2026, 9, 28, 0, 0, 10).timestamp()
        tl.log("next_day")

        assert _types(tmp_path / "cron_dream_2026-09-27.jsonl") == ["a", "b"]
        assert _types(tmp_path / "cron_dream_2026-09-28.jsonl") == ["next_day"]

    def test_a_thread_stamped_before_midnight_cannot_undo_another_threads_rollover(
        self, tmp_path: Path, clock, monkeypatch,
    ):
        """The gateway's logger is shared by threads. One thread stamps its
        event before midnight and is held up; another logs after midnight
        meanwhile. The held one must not move the logger back to the day
        before with a fresh count: that day's file is already at the cap."""
        from durin.telemetry import logger as tlog

        monkeypatch.setattr(tlog, "_MAX_EVENTS_PER_FILE", 2)
        tl = get_session_logger("gateway", base_dir=tmp_path)
        tl.log("a")
        tl.log("b")
        before_midnight = clock[0]
        after_midnight = datetime(2026, 9, 28, 0, 0, 10).timestamp()
        held_stamping = threading.Event()
        other_logged = threading.Event()

        def stamp() -> float:
            if threading.current_thread().name != "held":
                return after_midnight
            held_stamping.set()
            # Long enough for the other thread to log, unless the logger
            # makes it wait for this event to be written first.
            other_logged.wait(0.5)
            return before_midnight

        monkeypatch.setattr(tlog, "time", SimpleNamespace(time=stamp))

        def log_after_midnight() -> None:
            tl.log("after_midnight")
            other_logged.set()

        held = threading.Thread(target=tl.log, args=("held",), name="held")
        other = threading.Thread(target=log_after_midnight)
        held.start()
        assert held_stamping.wait(5)
        other.start()
        held.join(5)
        other.join(5)

        assert _types(tmp_path / "gateway_2026-09-27.jsonl") == ["a", "b"]
        assert _types(tmp_path / "gateway_2026-09-28.jsonl") == ["after_midnight"]
        assert tl.path == tmp_path / "gateway_2026-09-28.jsonl"

    def test_logger_with_a_fixed_path_never_moves(self, tmp_path: Path, clock):
        path = tmp_path / "fixed.jsonl"
        tl = TelemetryLogger(path)
        tl.log("one")
        clock[0] = datetime(2026, 9, 28, 0, 0, 10).timestamp()
        tl.log("two")

        assert _types(path) == ["one", "two"]
        assert [p.name for p in tmp_path.iterdir()] == ["fixed.jsonl"]
