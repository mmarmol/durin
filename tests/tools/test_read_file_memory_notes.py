import asyncio
import json
from datetime import date
from pathlib import Path

from durin.agent.tools.filesystem import ReadFileTool
from durin.memory.indexer import rebuild_fts_index
from durin.memory.session_summary_store import write_session_summary
from durin.telemetry.logger import TelemetryLogger, bind_telemetry, reset_telemetry


def _prepare(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n")
    write_session_summary(
        tmp_path, "websocket:old",
        "- app entry point reviewed\nFiles/paths examined in this span (read_file to reopen): src/app.py",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)


def test_read_file_appends_memory_notes(tmp_path: Path) -> None:
    _prepare(tmp_path)
    out = asyncio.run(ReadFileTool(workspace=tmp_path).execute(path="src/app.py"))
    assert "print('hi')" in out
    assert "Memory notes about this file" in out
    assert "app entry point reviewed" in out


def test_verbatim_read_has_no_notes(tmp_path: Path) -> None:
    _prepare(tmp_path)
    out = asyncio.run(ReadFileTool(workspace=tmp_path).execute(path="src/app.py", verbatim=True))
    assert "Memory notes" not in out


def test_read_file_telemetry_counts_the_notes_and_excludes_them_from_result_chars(
    tmp_path: Path,
) -> None:
    """Artifact recall must not pollute the search series, and the read event
    has to say how many notes it appended without counting them as content."""
    _prepare(tmp_path)
    log_path = tmp_path / "tel" / "events.jsonl"
    token = bind_telemetry(TelemetryLogger(log_path))
    try:
        out = asyncio.run(ReadFileTool(workspace=tmp_path).execute(path="src/app.py"))
    finally:
        reset_telemetry(token)

    events = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    assert [e for e in events if e["type"] == "memory.recall.lexical"] == []
    reads = [e for e in events if e["type"] == "tool.read_file"]
    assert len(reads) == 1
    data = reads[0]["data"]
    assert data["memory_notes"] == 1
    assert data["result_chars"] == out.index("\n\nMemory notes about this file")


def test_read_file_without_notes_reports_zero(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n")
    log_path = tmp_path / "tel" / "events.jsonl"
    token = bind_telemetry(TelemetryLogger(log_path))
    try:
        out = asyncio.run(ReadFileTool(workspace=tmp_path).execute(path="src/app.py"))
    finally:
        reset_telemetry(token)

    events = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    data = next(e["data"] for e in events if e["type"] == "tool.read_file")
    assert data["memory_notes"] == 0
    assert data["result_chars"] == len(out)
