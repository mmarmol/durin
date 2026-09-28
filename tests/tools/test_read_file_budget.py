"""read_file sizes each page to the agent runner's per-result cap.

Anything over the cap is taken out of the model's context by the runner and
replaced with a short preview, which drops read_file's own "continue at"
footer. So a page must fit the cap on its own, and content that no line
window can split (one enormous line) must still be reachable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.context import reset_result_char_cap, set_result_char_cap
from durin.agent.tools.filesystem import ReadFileTool
from durin.utils.helpers import parse_persisted_reference


@pytest.fixture()
def small_cap():
    token = set_result_char_cap(8_000)
    yield 8_000
    reset_result_char_cap(token)


@pytest.mark.asyncio
async def test_a_one_line_file_larger_than_the_cap_can_be_read_to_the_end(tmp_path: Path, small_cap: int) -> None:
    line = "".join(f"{i:06d}," for i in range(30_000))  # 210,000 chars, one line
    f = tmp_path / "minified.json"
    f.write_text(line, encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)

    first = await tool.execute(path=str(f))
    assert len(first) <= small_cap
    assert "000000," in first  # content, not an empty "lines 1-0" page

    # Page through the line with the offsets the footers give.
    collected = []
    page = await tool.execute(path=str(f), offset=1, limit=1, char_offset=0)
    for _ in range(200):
        assert len(page) <= small_cap
        body = page.split("\n\n(")[0]
        collected.append(body.split("| ", 1)[1])
        match = re.search(r"char_offset=(\d+) to continue", page)
        if not match:
            break
        page = await tool.execute(path=str(f), offset=1, limit=1, char_offset=int(match.group(1)))
    assert "".join(collected) == line


@pytest.mark.asyncio
async def test_a_long_line_among_short_ones_is_shortened_with_a_pointer_to_the_rest(tmp_path: Path) -> None:
    lines = ["short a", "x" * 5_000, "short b"]
    f = tmp_path / "mixed.txt"
    f.write_text("\n".join(lines), encoding="utf-8")

    result = await ReadFileTool(workspace=tmp_path).execute(path=str(f))

    assert "1| short a" in result
    assert "3| short b" in result
    assert "x" * 5_000 not in result
    assert "offset=2, limit=1, char_offset=" in result


@pytest.mark.asyncio
async def test_a_long_file_is_served_in_pages_that_fit_the_cap(tmp_path: Path, small_cap: int) -> None:
    f = tmp_path / "log.txt"
    f.write_text("\n".join(f"entry {i}: " + "detail " * 10 for i in range(2_000)), encoding="utf-8")

    result = await ReadFileTool(workspace=tmp_path).execute(path=str(f))

    assert len(result) <= small_cap
    assert re.search(r"Use offset=\d+ to continue", result)


@pytest.mark.asyncio
async def test_a_batch_read_fits_the_cap_as_a_whole(tmp_path: Path, small_cap: int) -> None:
    paths = []
    for n in range(3):
        f = tmp_path / f"f{n}.txt"
        f.write_text("\n".join(f"file {n} line {i} " + "y" * 30 for i in range(1_000)), encoding="utf-8")
        paths.append(str(f))

    result = await ReadFileTool(workspace=tmp_path).execute(paths=paths)

    assert len(json.dumps(result, ensure_ascii=False)) <= small_cap
    for record in result["results"]:
        assert re.search(r"Use offset=\d+ to continue", record["content"])


@pytest.fixture()
def run_cap():
    token = set_result_char_cap(16_000)
    yield 16_000
    reset_result_char_cap(token)


@pytest.mark.asyncio
async def test_a_batch_page_cut_to_its_share_does_not_make_a_later_read_unchanged(tmp_path: Path, run_cap: int) -> None:
    paths = []
    for k in range(15):
        f = tmp_path / f"mod{k}.py"
        f.write_text("\n".join(f"def f{i}(): return {i}  # module {k}" for i in range(800)), encoding="utf-8")
        paths.append(str(f))
    tool = ReadFileTool(workspace=tmp_path)

    batch = await tool.execute(paths=paths)
    assert "Use offset=" in batch["results"][3]["content"]

    single = await tool.execute(path=paths[3])

    assert "unchanged since last read" not in single
    assert "def f100(): return 100  # module 3" in single


@pytest.mark.asyncio
async def test_in_a_two_file_batch_only_the_cut_page_loses_its_dedup(tmp_path: Path, run_cap: int) -> None:
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"big line {i} " + "x" * 40 for i in range(180)), encoding="utf-8")  # ~9,900 chars
    small = tmp_path / "small.txt"
    small.write_text("\n".join(f"small line {i}" for i in range(20)), encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)

    batch = await tool.execute(paths=[str(big), str(small)])
    big_page, small_page = (record["content"] for record in batch["results"])
    assert "Use offset=" in big_page
    assert "(End of file" in small_page

    again_big = await tool.execute(path=str(big))
    assert "unchanged since last read" not in again_big
    assert "big line 150 " in again_big
    # The small file was shown whole, so a repeat read may say so.
    assert "unchanged since last read" in await tool.execute(path=str(small))


def _as_delivered(result: Any, cap: int, workspace: Path) -> Any:
    """What the model receives of a tool result: the runner's normalization,
    which saves anything over the cap to disk and sends a preview instead."""
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=cap,
        workspace=workspace,
        session_key="test:batch",
    )
    return AgentRunner(MagicMock())._normalize_tool_result(spec, "call_1", "read_file", result)


async def _batch(paths: list[str], cap: int, workspace: Path) -> dict[str, Any]:
    token = set_result_char_cap(cap)
    try:
        return await ReadFileTool(workspace=workspace).execute(paths=paths)
    finally:
        reset_result_char_cap(token)


_QUOTED_CSV = "\n".join(",".join(f'"{c}{i}"' for c in "abcdefgh") for i in range(4_000))


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "count"), [(16_000, 2), (16_000, 3), (16_000, 5), (8_000, 2)])
async def test_a_batch_of_quote_heavy_files_arrives_whole(tmp_path: Path, cap: int, count: int) -> None:
    paths = []
    for n in range(count):
        f = tmp_path / f"rows{n}.csv"
        f.write_text(_QUOTED_CSV, encoding="utf-8")
        paths.append(str(f))

    result = await _batch(paths, cap, tmp_path)
    delivered = _as_delivered(result, cap, tmp_path)

    assert parse_persisted_reference(delivered) is None
    assert len(delivered) <= cap
    for record in result["results"]:
        assert re.search(r"Use offset=\d+ to continue", record["content"])


@pytest.mark.asyncio
async def test_a_batch_of_documents_arrives_whole(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    paths = []
    for n in range(2):
        document = docx.Document()
        for i in range(600):
            document.add_paragraph(f'Paragraph {i} of "document {n}": the quick brown fox.')
        f = tmp_path / f"doc{n}.docx"
        document.save(str(f))
        paths.append(str(f))

    result = await _batch(paths, 16_000, tmp_path)
    delivered = _as_delivered(result, 16_000, tmp_path)

    assert parse_persisted_reference(delivered) is None
    assert len(delivered) <= 16_000
    for n, record in enumerate(result["results"]):
        assert f'Paragraph 0 of "document {n}"' in record["content"]
        assert "Document text cut at" in record["content"]


@pytest.mark.asyncio
async def test_char_offset_zero_on_a_short_line_reads_the_normal_page(tmp_path: Path, run_cap: int) -> None:
    f = tmp_path / "mod.py"
    f.write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")

    explicit = await ReadFileTool(workspace=tmp_path).execute(path=str(f), char_offset=0)
    plain = await ReadFileTool(workspace=tmp_path).execute(path=str(f))

    assert explicit == plain
    assert "(End of file — 500 lines total)" in explicit


@pytest.mark.asyncio
async def test_outside_an_agent_run_the_historical_limit_applies(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("\n".join("z" * 50 for _ in range(1_000)), encoding="utf-8")  # ~51,000 chars

    result = await ReadFileTool(workspace=tmp_path).execute(path=str(f))

    assert "(End of file" in result
    assert len(result) > 16_000


_DOC_SECRET = "doc-secret-value-0123456789"
_DOC_TAIL = "THE_LAST_PARAGRAPH"


def _fake_long_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """A document whose extracted text is far over one page, holding a
    stored secret and a marker at its very end."""
    import durin.security.secrets as secrets
    import durin.utils.document as document
    from durin.security.secrets import SecretRedactor

    body = "\n".join(f"Paragraph {i}: the quick brown fox jumps over the lazy dog." for i in range(1_200))
    text = f"{body}\nkey {_DOC_SECRET}\n{_DOC_TAIL}"
    monkeypatch.setattr(document, "extract_text", lambda fp: text)
    monkeypatch.setattr(secrets, "build_redactor", lambda: SecretRedactor({"DOC_KEY": _DOC_SECRET}))


async def _read_to_the_end(tool: ReadFileTool, path: str, offset: int = 1) -> str:
    """Follow read_file's own continuation footers from ``offset`` on."""
    pages: list[str] = []
    for _ in range(40):
        page = await tool.execute(path=path, offset=offset)
        pages.append(page)
        nxt = re.search(r"Use offset=(\d+) to continue", page)
        if nxt is None:
            break
        offset = int(nxt.group(1))
    return "\n".join(pages)


@pytest.mark.asyncio
async def test_an_office_document_over_its_page_is_saved_and_read_back(
    tmp_path: Path, run_cap: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_long_document(monkeypatch)
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"PK")
    tool = ReadFileTool(workspace=tmp_path)

    page = await tool.execute(path=str(doc))

    assert parse_persisted_reference(_as_delivered(page, run_cap, tmp_path)) is None
    assert "Document text cut at" in page
    assert "convert_to_markdown" not in page
    saved = re.search(r'read_file\(path="([^"]+)", offset=(\d+)\)', page)
    assert saved is not None
    path, offset = saved.group(1), int(saved.group(2))
    assert _DOC_SECRET not in Path(path).read_text(encoding="utf-8")
    # The pointer continues where the page stopped: nothing the page showed
    # is read twice as a whole page, and nothing is skipped.
    assert offset > 1
    rest = await _read_to_the_end(tool, path, offset)
    assert _DOC_TAIL in rest
    assert all(f"Paragraph {i}:" in page + rest for i in range(1_200))


@pytest.mark.asyncio
async def test_reading_the_same_document_again_reuses_its_saved_text(
    tmp_path: Path, run_cap: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    _fake_long_document(monkeypatch)
    ticks = iter(range(1_000_000, 2_000_000, 7))
    monkeypatch.setattr(time, "time", lambda: float(next(ticks)))
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"PK")
    tool = ReadFileTool(workspace=tmp_path)

    first = await tool.execute(path=str(doc))
    second = await tool.execute(path=str(doc))

    pointer = re.compile(r'read_file\(path="([^"]+)"')
    assert pointer.search(first).group(1) == pointer.search(second).group(1)
    assert len(list((tmp_path / ".durin" / "spills").iterdir())) == 1


@pytest.mark.asyncio
async def test_a_page_too_small_for_its_pointer_never_returns_the_whole_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_long_document(monkeypatch)
    workspace = tmp_path / ("w" * 200) / ("x" * 200)
    workspace.mkdir(parents=True)
    doc = workspace / "report.docx"
    doc.write_bytes(b"PK")
    token = set_result_char_cap(1_000)
    try:
        page = await ReadFileTool(workspace=workspace).execute(path=str(doc))
    finally:
        reset_result_char_cap(token)

    assert "Document text cut at 0 of" in page
    assert len(page) < 2_000


@pytest.mark.asyncio
async def test_a_batch_with_a_document_over_its_share_still_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_long_document(monkeypatch)
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"PK")
    small = tmp_path / "notes.txt"
    small.write_text("a short note", encoding="utf-8")

    result = await _batch([str(doc), str(small)], 16_000, tmp_path)
    delivered = _as_delivered(result, 16_000, tmp_path)

    assert parse_persisted_reference(delivered) is None
    assert len(delivered) <= 16_000
    assert 'read_file(path=\\"' in json.dumps(result["results"][0]["content"])


@pytest.mark.asyncio
async def test_an_office_document_that_cannot_be_saved_says_why(
    tmp_path: Path, run_cap: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import durin.agent.tools.output_spill as output_spill

    _fake_long_document(monkeypatch)

    def _refuse(path: Path, content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(output_spill, "atomic_write_text", _refuse)
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"PK")

    page = await ReadFileTool(workspace=tmp_path).execute(path=str(doc))

    assert "could not be saved" in page
    assert "disk full" in page
