"""Tests for structured telemetry logger."""

from __future__ import annotations

import json
from pathlib import Path

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
