"""A persisted tool result tells the model how to get the rest of it."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from durin.agent.tools.context import reset_result_char_cap, set_result_char_cap
from durin.agent.tools.filesystem import ReadFileTool
from durin.agent.tools.search import GrepTool
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


@pytest.mark.asyncio
async def test_following_the_grep_hint_literally_returns_the_matching_lines(tmp_path: Path) -> None:
    ref = maybe_persist_tool_result(
        tmp_path, "sess", "call_1", BIG + "\nTHE_FACT is here\n", max_chars=16_000,
    )
    hint = re.search(r"grep\(pattern=\.\.\., ([^)]*)\)", ref)
    assert hint is not None, ref
    arguments = dict(re.findall(r'(\w+)="([^"]*)"', hint.group(1)))

    result = await GrepTool(workspace=tmp_path).execute(pattern="THE_FACT", **arguments)

    assert "THE_FACT is here" in result


def _saved_path(ref: object) -> Path:
    parsed = parse_persisted_reference(ref)
    assert parsed is not None
    return Path(parsed[0])


def test_a_one_line_json_result_is_saved_as_lines(tmp_path: Path) -> None:
    rows = [{"id": i, "note": f"row {i} " + "x" * 40} for i in range(400)]
    text = json.dumps({"rows": rows}, ensure_ascii=False)

    saved = _saved_path(maybe_persist_tool_result(tmp_path, "sess", "call_1", text, max_chars=16_000))

    lines = saved.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 400
    assert any(line.strip().startswith("note: row 5 ") for line in lines)


def test_one_line_text_that_is_not_json_is_saved_as_is(tmp_path: Path) -> None:
    text = "{not json " + "y" * 20_000

    saved = _saved_path(maybe_persist_tool_result(tmp_path, "sess", "call_1", text, max_chars=16_000))

    assert saved.read_text(encoding="utf-8") == text


def test_a_result_saved_again_unchanged_is_not_rewritten(tmp_path: Path) -> None:
    saved = _saved_path(maybe_persist_tool_result(tmp_path, "sess", "call_1", BIG, max_chars=16_000))
    old = 1_000_000_000
    os.utime(saved, (old, old))
    os.utime(saved.parent, (old, old))

    maybe_persist_tool_result(tmp_path, "sess", "call_1", BIG, max_chars=16_000)

    assert saved.stat().st_mtime == old
    # The bucket still counts as in use, as a rewrite would have marked it,
    # so cleanup does not take it for stale.
    assert saved.parent.stat().st_mtime > old


def test_a_changed_result_under_the_same_call_id_is_rewritten(tmp_path: Path) -> None:
    maybe_persist_tool_result(tmp_path, "sess", "call_1", BIG, max_chars=16_000)

    saved = _saved_path(
        maybe_persist_tool_result(tmp_path, "sess", "call_1", BIG + "\nnew row", max_chars=16_000)
    )

    assert saved.read_text(encoding="utf-8").endswith("new row")
