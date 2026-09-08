import asyncio
from datetime import date
from pathlib import Path

from durin.agent.tools.filesystem import ReadFileTool
from durin.memory.indexer import rebuild_fts_index
from durin.memory.session_summary_store import write_session_summary


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
