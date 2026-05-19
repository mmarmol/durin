"""Tests for tool-level telemetry events (Phase 1c — Tool I/O hygiene).

These verify that ReadFileTool and GrepTool emit `tool.read_file` and `tool.grep`
JSONL events when a telemetry logger is bound to the current task, and silently
no-op when it isn't.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from durin.agent.tools import file_state
from durin.agent.tools.filesystem import ReadFileTool
from durin.agent.tools.search import GrepTool
from durin.telemetry.logger import (
    TelemetryLogger,
    bind_telemetry,
    reset_telemetry,
)


@pytest.fixture(autouse=True)
def _clear_file_state():
    file_state.clear()
    yield
    file_state.clear()


@pytest.fixture
def telemetry_log(tmp_path: Path) -> tuple[TelemetryLogger, Path]:
    log_path = tmp_path / "telemetry.jsonl"
    return TelemetryLogger(log_path), log_path


def _read_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# ReadFileTool
# ---------------------------------------------------------------------------


class TestReadFileTelemetry:

    def test_emits_event_on_successful_read(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        target = tmp_path / "hello.txt"
        target.write_text("line1\nline2\nline3\n")

        tool = ReadFileTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(path="hello.txt"))
        finally:
            reset_telemetry(token)

        events = _read_events(log_path)
        assert len(events) == 1
        evt = events[0]
        assert evt["type"] == "tool.read_file"
        data = evt["data"]
        assert data["path"] == "hello.txt"
        assert data["total_lines"] == 3
        assert data["returned_lines"] == 3
        assert data["truncated"] is False
        assert data["dedup"] is False
        assert data["kind"] == "text"

    def test_truncated_event_when_limit_smaller_than_file(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        target = tmp_path / "big.txt"
        target.write_text("\n".join(f"line{i}" for i in range(100)) + "\n")

        tool = ReadFileTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(path="big.txt", offset=1, limit=10))
        finally:
            reset_telemetry(token)

        events = _read_events(log_path)
        assert len(events) == 1
        data = events[0]["data"]
        assert data["total_lines"] == 100
        assert data["returned_lines"] == 10
        assert data["truncated"] is True
        assert data["limit"] == 10

    def test_dedup_event_on_repeated_read(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        target = tmp_path / "twice.txt"
        target.write_text("only content\n")

        tool = ReadFileTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(path="twice.txt"))
            asyncio.run(tool.execute(path="twice.txt"))
        finally:
            reset_telemetry(token)

        events = _read_events(log_path)
        assert len(events) == 2
        assert events[0]["data"]["dedup"] is False
        assert events[1]["data"]["dedup"] is True

    def test_no_logger_bound_does_not_crash(self, tmp_path: Path):
        target = tmp_path / "no_log.txt"
        target.write_text("content\n")

        tool = ReadFileTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(path="no_log.txt"))
        assert "content" in result

    def test_read_error_does_not_emit(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        tool = ReadFileTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        try:
            result = asyncio.run(tool.execute(path="missing.txt"))
        finally:
            reset_telemetry(token)
        assert "Error" in result
        assert _read_events(log_path) == []


# ---------------------------------------------------------------------------
# GrepTool
# ---------------------------------------------------------------------------


class TestGrepTelemetry:

    def test_emits_event_on_files_with_matches(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        (tmp_path / "a.py").write_text("hello = 1\nworld = 2\n")
        (tmp_path / "b.py").write_text("hello = 3\n")
        (tmp_path / "c.txt").write_text("no relevant line\n")

        tool = GrepTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(pattern="hello", path="."))
        finally:
            reset_telemetry(token)

        events = _read_events(log_path)
        assert len(events) == 1
        data = events[0]["data"]
        assert events[0]["type"] == "tool.grep"
        assert data["output_mode"] == "files_with_matches"
        assert data["displayed"] == 2
        assert data["total_before_pagination"] == 2
        assert data["truncated"] is False
        assert data["pattern_len"] == 5

    def test_emits_truncation_flag_when_head_limit_hit(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text("needle\n")

        tool = GrepTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(pattern="needle", path=".", head_limit=2))
        finally:
            reset_telemetry(token)

        events = _read_events(log_path)
        assert len(events) == 1
        data = events[0]["data"]
        assert data["total_before_pagination"] == 5
        assert data["displayed"] == 2
        assert data["truncated"] is True

    def test_no_matches_still_emits_with_zero_counts(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        (tmp_path / "a.py").write_text("nothing here\n")

        tool = GrepTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(pattern="needle", path="."))
        finally:
            reset_telemetry(token)

        events = _read_events(log_path)
        assert len(events) == 1
        data = events[0]["data"]
        assert data["displayed"] == 0
        assert data["total_before_pagination"] == 0
        assert data["truncated"] is False

    def test_no_logger_bound_does_not_crash(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("hello\n")
        tool = GrepTool(workspace=tmp_path)
        result = asyncio.run(tool.execute(pattern="hello", path="."))
        assert "a.py" in result


# ---------------------------------------------------------------------------
# ContextVar isolation
# ---------------------------------------------------------------------------


class TestTelemetryContextVarIsolation:

    def test_reset_unbinds_logger(self, tmp_path: Path, telemetry_log):
        logger, log_path = telemetry_log
        (tmp_path / "f.txt").write_text("x\n")

        tool = ReadFileTool(workspace=tmp_path)
        token = bind_telemetry(logger)
        asyncio.run(tool.execute(path="f.txt"))
        reset_telemetry(token)

        # Second call after reset should not log anything new.
        before = len(_read_events(log_path))
        asyncio.run(tool.execute(path="f.txt"))
        after = len(_read_events(log_path))
        assert before == after
