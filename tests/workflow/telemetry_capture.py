"""Reads back the telemetry a test's code recorded: the instance's telemetry
folder is the test's own, and every event is a JSON line in it."""
import json
from pathlib import Path

import pytest


@pytest.fixture(name="telemetry_dir")
def telemetry_dir_fixture(tmp_path, monkeypatch) -> Path:
    """The test's own instance: its telemetry folder (named ``telemetry_dir``
    in a test's arguments)."""
    home = tmp_path / "events-home"
    monkeypatch.setenv("DURIN_HOME", str(home))
    return home / "telemetry"


def recorded(telemetry_dir: Path, prefix: str = "workflow.") -> list[dict]:
    """The events recorded under ``telemetry_dir`` whose type starts with
    ``prefix``, as ``{"type", "data", ...}`` rows in file order."""
    if not telemetry_dir.is_dir():
        return []
    rows = []
    for f in sorted(telemetry_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["type"].startswith(prefix):
                rows.append(row)
    return rows


def payloads(telemetry_dir: Path, event: str) -> list[dict]:
    """The data of each ``event`` recorded, without the session and iteration
    the logger stamps on every event."""
    return [{k: v for k, v in r["data"].items() if k not in ("session_key", "iteration")}
            for r in recorded(telemetry_dir, event) if r["type"] == event]


def assert_clean(rows: list[dict], *private: str) -> None:
    """No payload holds a path, a pid, a session key or any of ``private``."""
    for row in rows:
        data = dict(row["data"])
        data.pop("session_key", None)       # stamped by the logger, not by the event
        data.pop("iteration", None)
        text = json.dumps(data)
        assert "/" not in text and "pid" not in text, text
        for word in private:
            assert word not in text, text


@pytest.fixture(name="log_lines")
def log_lines_fixture():
    """The messages durin's workflow modules log while the test runs, at INFO
    and above."""
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO",
                      filter="durin.workflow", format="{message}")
    yield lines
    logger.remove(sink)
