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
async def test_outside_an_agent_run_the_historical_limit_applies(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("\n".join("z" * 50 for _ in range(1_000)), encoding="utf-8")  # ~51,000 chars

    result = await ReadFileTool(workspace=tmp_path).execute(path=str(f))

    assert "(End of file" in result
    assert len(result) > 16_000
