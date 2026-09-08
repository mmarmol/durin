import asyncio
import json
from datetime import date
from pathlib import Path

from durin.agent.tools.filesystem import ReadFileTool
from durin.memory.indexer import rebuild_fts_index
from durin.memory.session_summary_store import write_session_summary
from durin.telemetry.logger import TelemetryLogger, bind_telemetry, reset_telemetry

_HEADER = "Memory notes about this file"


def _prepare(tmp_path: Path, body: str = "print('hi')\n") -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(body)
    write_session_summary(
        tmp_path, "websocket:old",
        "- app entry point reviewed\nFiles/paths examined in this span (read_file to reopen): src/app.py",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)


def test_read_file_prepends_memory_notes(tmp_path: Path) -> None:
    _prepare(tmp_path)
    out = asyncio.run(ReadFileTool(workspace=tmp_path).execute(path="src/app.py"))
    assert out.startswith(_HEADER)
    assert "app entry point reviewed" in out
    assert "1| print('hi')" in out
    # The notes block ends with a blank line, then the numbered content starts.
    assert out.split("\n\n", 1)[1].startswith("1| print('hi')")


def test_verbatim_read_has_no_notes(tmp_path: Path) -> None:
    _prepare(tmp_path)
    out = asyncio.run(ReadFileTool(workspace=tmp_path).execute(path="src/app.py", verbatim=True))
    assert "Memory notes" not in out


def test_read_file_telemetry_counts_the_notes_and_excludes_them_from_result_chars(
    tmp_path: Path,
) -> None:
    """Artifact recall must not pollute the search series, and the read event
    has to say how many notes it added without counting them as content."""
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
    assert data["result_chars"] == len(out) - out.index("1| print('hi')")


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


def test_notes_survive_the_loop_truncation_of_a_long_file(tmp_path: Path) -> None:
    """A file longer than the loop's tool-result cap keeps its notes block:
    the loop truncates from the tail, so the block has to lead."""
    from durin.agent.loop import _truncate_tool_output

    cap = 4_000
    _prepare(tmp_path, body="print('hi')\n" + "x = 1\n" * 2000)

    out = asyncio.run(ReadFileTool(workspace=tmp_path).execute(path="src/app.py"))
    assert len(out) > cap

    truncated = _truncate_tool_output(out, cap, "read_file")
    assert truncated.startswith(_HEADER)
    assert "app entry point reviewed" in truncated
