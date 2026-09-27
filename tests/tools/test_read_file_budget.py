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

import pytest

from durin.agent.tools.context import reset_result_char_cap, set_result_char_cap
from durin.agent.tools.filesystem import ReadFileTool


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


@pytest.mark.asyncio
async def test_outside_an_agent_run_the_historical_limit_applies(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("\n".join("z" * 50 for _ in range(1_000)), encoding="utf-8")  # ~51,000 chars

    result = await ReadFileTool(workspace=tmp_path).execute(path=str(f))

    assert "(End of file" in result
    assert len(result) > 16_000
