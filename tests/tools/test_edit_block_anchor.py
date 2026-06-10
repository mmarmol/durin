"""Tests for the block-anchor replacer + edit telemetry (Sprint A / T2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from durin.agent.tools import file_state
from durin.agent.tools.filesystem import (
    EditFileTool,
    _find_block_anchor_matches,
    _find_matches_with_strategy,
)
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


def _read_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# _find_block_anchor_matches unit tests
# ---------------------------------------------------------------------------


class TestBlockAnchorUnit:

    def test_requires_three_or_more_lines(self):
        # 2-line old_text should not match via block-anchor
        content = "def foo():\n    return 1\n"
        old = "def foo():\n    return 1"
        m = _find_block_anchor_matches(content, old)
        assert m == []

    def test_finds_block_when_middle_lines_differ_slightly(self):
        content = (
            "def foo(x):\n"
            "    # slightly different comment\n"
            "    if x > 0:\n"
            "        return x\n"
            "    return 0\n"
        )
        # old_text has same first+last anchor, middle slightly off
        old = (
            "def foo(x):\n"
            "    # original comment text here\n"
            "    if x > 0:\n"
            "        return x\n"
            "    return 0"
        )
        m = _find_block_anchor_matches(content, old)
        assert len(m) == 1
        # Match span covers the actual content from `def foo` to `return 0`
        assert "def foo(x):" in m[0].text
        assert "return 0" in m[0].text

    def test_picks_first_when_multiple_candidates_strict_threshold(self):
        # Two blocks with identical anchors but different middles.
        # Strict threshold (0.85) → only the close match should qualify.
        content = (
            "def foo():\n"
            "    completely unrelated content here\n"
            "    return 1\n"
            "\n"
            "def foo():\n"
            "    matching middle content for old\n"
            "    return 1\n"
        )
        old = (
            "def foo():\n"
            "    matching middle content for old\n"
            "    return 1"
        )
        m = _find_block_anchor_matches(content, old)
        # Strict threshold: only block 2 should qualify
        assert len(m) == 1
        assert "matching middle content for old" in m[0].text

    def test_empty_anchors_rejected(self):
        # If first or last line is empty (after strip), no match attempted
        content = "\nmiddle\n\nfoo\n"
        old = "\nmiddle\n"  # only 2 lines, but also empty anchors
        m = _find_block_anchor_matches(content, old)
        assert m == []

    def test_truncated_middle_rejected(self):
        # Candidate middle is a truncated version of old_text's middle
        # (the file has enough total lines, but the block lost two calls).
        # The old metric scored this 1.0 (check_len=min() only compared the
        # first line); containment scores 0.61 < 0.66 → must NOT match.
        content = (
            "import logging\n"
            "\n"
            "def setup():\n"
            "    init_logging()\n"
            "    return ctx\n"
            "\n"
            "def teardown():\n"
            "    pass\n"
        )
        old = (
            "def setup():\n"
            "    init_logging()\n"
            "    connect_db()\n"
            "    load_config()\n"
            "    return ctx"
        )
        m = _find_block_anchor_matches(content, old)
        assert m == []

    def test_single_candidate_rewritten_body_rejected(self):
        # Same anchors, completely rewritten body (containment ~0.34).
        # Already rejected by the old 0.5 threshold — kept as a regression
        # guard for the new metric.
        content = (
            "def total(items):\n"
            "    log.info(\"called\")\n"
            "    raise NotImplementedError\n"
            "    return result\n"
        )
        old = (
            "def total(items):\n"
            "    total = sum(items)\n"
            "    return total / len(items)\n"
            "    return result"
        )
        m = _find_block_anchor_matches(content, old)
        assert m == []

    def test_single_candidate_inserted_lines_match(self):
        # Old middle lines all exist verbatim in the candidate; two extra
        # comment lines were inserted (containment 1.0).
        content = (
            "class C:\n"
            "    def __init__(self):\n"
            "        # extra comment that\n"
            "        # the model didn't see\n"
            "        self.x = 1\n"
            "    def m(self):\n"
        )
        old = (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "    def m(self):"
        )
        m = _find_block_anchor_matches(content, old)
        assert len(m) == 1


# ---------------------------------------------------------------------------
# Cascade strategy detection
# ---------------------------------------------------------------------------


class TestCascadeStrategy:

    def test_exact_match_reports_exact(self):
        matches, strategy = _find_matches_with_strategy("abc\ndef\n", "abc")
        assert len(matches) == 1
        assert strategy == "exact"

    def test_trim_match_reports_line_trimmed(self):
        content = "    foo()\n    bar()\n"
        old = "foo()\nbar()"
        matches, strategy = _find_matches_with_strategy(content, old)
        assert len(matches) == 1
        assert strategy == "line_trimmed"

    def test_block_anchor_match_reports_block_anchor(self):
        content = (
            "class Widget:\n"
            "    def __init__(self):\n"
            "        # custom init\n"
            "        self.x = 1\n"
            "        self.y = 2\n"
            "    def render(self):\n"
            "        pass\n"
        )
        # Anchors match, middle differs (extra line, comment phrasing)
        old = (
            "class Widget:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "        self.y = 2\n"
            "    def render(self):"
        )
        matches, strategy = _find_matches_with_strategy(content, old)
        # Trim strategies will fail because the actual file has the extra
        # "# custom init" line that's not in old_text — only block-anchor
        # can match this (anchors agree, middle is fuzzy).
        assert len(matches) == 1
        assert strategy == "block_anchor"


# ---------------------------------------------------------------------------
# EditFileTool integration
# ---------------------------------------------------------------------------


class TestEditFileBlockAnchorIntegration:

    @pytest.mark.asyncio
    async def test_edit_via_block_anchor_succeeds(self, tmp_path: Path):
        target = tmp_path / "code.py"
        target.write_text(
            "class C:\n"
            "    def __init__(self):\n"
            "        # extra comment that\n"
            "        # the model didn't see\n"
            "        self.x = 1\n"
            "    def m(self):\n"
            "        return 0\n"
        )
        tool = EditFileTool(workspace=tmp_path)
        # Pre-read so the read-before-edit guard is satisfied
        from durin.agent.tools.filesystem import ReadFileTool
        await ReadFileTool(workspace=tmp_path).execute(path="code.py")

        old = (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "    def m(self):"
        )
        new = (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.x = 2\n"
            "    def m(self):"
        )
        result = await tool.execute(path="code.py", old_text=old, new_text=new)
        assert "Successfully edited" in result
        body = target.read_text()
        # The new_text replaces the *entire* matched block, so the original
        # middle (extra comments) is replaced too. Block-anchor's job is to
        # FIND the span; the LLM is responsible for sending new_text with
        # the desired middle. Same semantics as OpenCode's BlockAnchorReplacer.
        assert "self.x = 2" in body
        assert "self.x = 1" not in body


# ---------------------------------------------------------------------------
# Edit telemetry
# ---------------------------------------------------------------------------


class TestEditTelemetry:

    @pytest.mark.asyncio
    async def test_successful_edit_emits_event_with_strategy(self, tmp_path: Path):
        logger = TelemetryLogger(tmp_path / "tel.jsonl")
        target = tmp_path / "f.py"
        target.write_text("foo = 1\nbar = 2\n")
        tool = EditFileTool(workspace=tmp_path)
        from durin.agent.tools.filesystem import ReadFileTool
        await ReadFileTool(workspace=tmp_path).execute(path="f.py")

        token = bind_telemetry(logger)
        try:
            await tool.execute(path="f.py", old_text="foo = 1", new_text="foo = 99")
        finally:
            reset_telemetry(token)

        events = _read_events(tmp_path / "tel.jsonl")
        edit_events = [e for e in events if e["type"] == "tool.edit_file"]
        assert len(edit_events) == 1
        data = edit_events[0]["data"]
        assert data["outcome"] == "edited"
        assert data["match_strategy"] == "exact"
        assert data["matches"] == 1

    @pytest.mark.asyncio
    async def test_not_found_emits_event_with_none_strategy(self, tmp_path: Path):
        logger = TelemetryLogger(tmp_path / "tel.jsonl")
        target = tmp_path / "f.py"
        target.write_text("x = 1\n")
        tool = EditFileTool(workspace=tmp_path)
        from durin.agent.tools.filesystem import ReadFileTool
        await ReadFileTool(workspace=tmp_path).execute(path="f.py")

        token = bind_telemetry(logger)
        try:
            await tool.execute(path="f.py", old_text="nope = 42", new_text="x")
        finally:
            reset_telemetry(token)

        events = _read_events(tmp_path / "tel.jsonl")
        edit_events = [e for e in events if e["type"] == "tool.edit_file"]
        assert len(edit_events) == 1
        data = edit_events[0]["data"]
        assert data["outcome"] == "not_found"
        assert data["match_strategy"] is None

    @pytest.mark.asyncio
    async def test_ambiguous_match_emits_event(self, tmp_path: Path):
        logger = TelemetryLogger(tmp_path / "tel.jsonl")
        target = tmp_path / "f.py"
        target.write_text("x = 1\nx = 1\n")
        tool = EditFileTool(workspace=tmp_path)
        from durin.agent.tools.filesystem import ReadFileTool
        await ReadFileTool(workspace=tmp_path).execute(path="f.py")

        token = bind_telemetry(logger)
        try:
            await tool.execute(path="f.py", old_text="x = 1", new_text="x = 2")
        finally:
            reset_telemetry(token)

        events = _read_events(tmp_path / "tel.jsonl")
        edit_events = [e for e in events if e["type"] == "tool.edit_file"]
        assert len(edit_events) == 1
        data = edit_events[0]["data"]
        assert data["outcome"] == "ambiguous"
        assert data["matches"] == 2


# ---------------------------------------------------------------------------
# Fuzzy-match transparency (Task 6 — tool-quality-fixes plan)
# ---------------------------------------------------------------------------


class TestFuzzyMatchNotice:

    @pytest.mark.asyncio
    async def test_fuzzy_strategy_noted_in_message(self, tmp_path: Path):
        from durin.agent.tools.filesystem import ReadFileTool
        target = tmp_path / "f.py"
        target.write_text("    foo()\n    bar()\n", encoding="utf-8")
        await ReadFileTool(workspace=tmp_path).execute(path="f.py")
        tool = EditFileTool(workspace=tmp_path)
        # Unindented old_text only matches via the line_trimmed fallback.
        result = await tool.execute(
            path="f.py", old_text="foo()\nbar()", new_text="baz()",
        )
        assert "Successfully edited" in result
        assert "line_trimmed" in result

    @pytest.mark.asyncio
    async def test_exact_match_has_no_notice(self, tmp_path: Path):
        from durin.agent.tools.filesystem import ReadFileTool
        target = tmp_path / "f.py"
        target.write_text("x = 1\n", encoding="utf-8")
        await ReadFileTool(workspace=tmp_path).execute(path="f.py")
        tool = EditFileTool(workspace=tmp_path)
        result = await tool.execute(path="f.py", old_text="x = 1", new_text="x = 2")
        assert "Successfully edited" in result
        assert "fallback" not in result
