"""A persisted tool result tells the model how to get the rest of it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from durin.agent.tools.context import reset_result_char_cap, set_result_char_cap
from durin.agent.tools.filesystem import ReadFileTool
from durin.utils.helpers import maybe_persist_tool_result, parse_persisted_reference

BIG = "\n".join(f"row {i}: " + "value " * 12 for i in range(1_500))  # ~120,000 chars


def test_a_persisted_reference_says_how_to_read_it_and_not_to_rerun(tmp_path: Path) -> None:
    ref = maybe_persist_tool_result(tmp_path, "sess", "call_1", BIG, max_chars=16_000)

    parsed = parse_persisted_reference(ref)
    assert parsed is not None
    path, size = parsed
    assert size == len(BIG)
    assert f'read_file(path="{path}")' in ref
    assert re.search(r"(?i)do not re-run", ref)
    assert f"Lines: {len(BIG.splitlines())}" in ref


@pytest.mark.asyncio
async def test_following_a_persisted_reference_gives_a_page_that_fits(tmp_path: Path) -> None:
    ref = maybe_persist_tool_result(tmp_path, "sess", "call_1", BIG, max_chars=16_000)
    path, _ = parse_persisted_reference(ref)

    token = set_result_char_cap(16_000)
    try:
        page = await ReadFileTool(workspace=tmp_path).execute(path=path)
    finally:
        reset_result_char_cap(token)

    assert len(page) <= 16_000
    assert re.search(r"Use offset=\d+ to continue", page)
