"""Tests for grep search tools."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.agent.subagent import SubagentManager, SubagentStatus
from durin.agent.tools.search import GrepTool
from durin.agent.tools.web import WebSearchTool
from durin.bus.queue import MessageBus
from durin.config.schema import WebSearchConfig


@pytest.mark.asyncio
async def test_web_search_tool_refreshes_dynamic_config_loader(monkeypatch) -> None:
    tool = WebSearchTool(
        config=WebSearchConfig(provider="brave"),
        config_loader=lambda: WebSearchConfig(provider="duckduckgo", max_results=3),
    )

    async def fake_duckduckgo(self, query: str, n: int) -> str:
        return f"{self.config.provider}:{query}:{n}"

    monkeypatch.setattr(WebSearchTool, "_search_duckduckgo", fake_duckduckgo)

    assert await tool.execute("durin") == "duckduckgo:durin:3"


@pytest.mark.asyncio
async def test_grep_respects_glob_filter_and_context(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "alpha\nbeta\nmatch_here\ngamma\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("match_here\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="match_here",
        path=".",
        glob="*.py",
        output_mode="content",
        context_before=1,
        context_after=1,
    )

    assert "src/main.py:3" in result
    assert "  2| beta" in result
    assert "> 3| match_here" in result
    assert "  4| gamma" in result
    assert "README.md" not in result


@pytest.mark.asyncio
async def test_grep_defaults_to_files_with_matches(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("match_here\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="match_here",
        path="src",
    )

    assert result.splitlines() == ["src/main.py"]
    assert "1|" not in result


@pytest.mark.asyncio
async def test_grep_supports_case_insensitive_search(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "HISTORY.md").write_text(
        "[2026-04-02 10:00] OAuth token rotated\n",
        encoding="utf-8",
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="oauth",
        path="memory/HISTORY.md",
        case_insensitive=True,
        output_mode="content",
    )

    assert "memory/HISTORY.md:1" in result
    assert "OAuth token rotated" in result


@pytest.mark.asyncio
async def test_grep_type_filter_limits_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "src" / "b.md").write_text("needle\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        type="py",
    )

    assert result.splitlines() == ["src/a.py"]


@pytest.mark.asyncio
async def test_grep_fixed_strings_treats_regex_chars_literally(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "HISTORY.md").write_text(
        "[2026-04-02 10:00] OAuth token rotated\n",
        encoding="utf-8",
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="[2026-04-02 10:00]",
        path="memory/HISTORY.md",
        fixed_strings=True,
        output_mode="content",
    )

    assert "memory/HISTORY.md:1" in result
    assert "[2026-04-02 10:00] OAuth token rotated" in result


@pytest.mark.asyncio
async def test_grep_files_with_matches_mode_returns_unique_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    a = tmp_path / "src" / "a.py"
    b = tmp_path / "src" / "b.py"
    a.write_text("needle\nneedle\n", encoding="utf-8")
    b.write_text("needle\n", encoding="utf-8")
    os.utime(a, (1, 1))
    os.utime(b, (2, 2))

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        output_mode="files_with_matches",
    )

    assert result.splitlines() == ["src/b.py", "src/a.py"]


@pytest.mark.asyncio
async def test_grep_files_with_matches_supports_head_limit_and_offset(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / "src" / name).write_text("needle\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        head_limit=1,
        offset=1,
    )

    # Filesystem order is not deterministic across platforms, so just verify:
    # 1. Only one file path is returned (head_limit=1 after offset=1)
    # 2. The note gives the position, the total and where to continue
    assert "(showing 2-2 of 3; use offset=2 to continue)" in result
    # Count non-empty lines that start with src/ (file paths)
    file_lines = [line for line in result.splitlines() if line.startswith("src/")]
    assert len(file_lines) == 1


@pytest.mark.asyncio
async def test_grep_count_mode_reports_counts_per_file(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "one.log").write_text("warn\nok\nwarn\n", encoding="utf-8")
    (tmp_path / "logs" / "two.log").write_text("warn\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="warn",
        path="logs",
        output_mode="count",
    )

    assert "logs/one.log: 2" in result
    assert "logs/two.log: 1" in result
    assert "total matches: 3 in 2 files" in result


@pytest.mark.asyncio
async def test_grep_files_with_matches_mode_respects_max_results(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    files = []
    for idx, name in enumerate(("a.py", "b.py", "c.py"), start=1):
        file_path = tmp_path / "src" / name
        file_path.write_text("needle\n", encoding="utf-8")
        os.utime(file_path, (idx, idx))
        files.append(file_path)

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        output_mode="files_with_matches",
        max_results=2,
    )

    assert result.splitlines()[:2] == ["src/c.py", "src/b.py"]
    assert "(showing 1-2 of 3; use offset=2 to continue)" in result


@pytest.mark.asyncio
async def test_content_mode_limit_note_gives_the_next_offset(tmp_path: Path) -> None:
    for n in range(5):
        (tmp_path / f"f{n}.txt").write_text("needle here\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="needle", path=".", output_mode="content", head_limit=2)

    assert "use offset=2" in result


@pytest.mark.asyncio
async def test_a_huge_matching_line_is_shown_shortened_not_as_no_match(tmp_path: Path) -> None:
    (tmp_path / "min.json").write_text("{" + '"k": "v", ' * 20_000 + '"needle": 1}', encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="needle", path=".", output_mode="content")

    assert "No matches found" not in result
    assert "char_offset=" in result
    assert len(result) < 20_000


_LINE_POINTER = re.compile(r'read_file\(path="([^"]+)", offset=(\d+), limit=1, char_offset=(\d+)\)')
_FACT_AT = 45_001


@pytest.fixture()
def run_cap():
    from durin.agent.tools.context import reset_result_char_cap, set_result_char_cap

    token = set_result_char_cap(16_000)
    yield 16_000
    reset_result_char_cap(token)


def _one_line_saved_output(workspace: Path) -> Path:
    """A saved tool output that is one 67,751-char line, its only match deep inside."""
    saved = workspace / ".durin" / "tool-results" / "slack_T1" / "call_1.txt"
    saved.parent.mkdir(parents=True)
    saved.write_text("x" * (_FACT_AT - 1) + " THE_FACT_42 " + "y" * 22_738, encoding="utf-8")
    return saved


def _session_tools(workspace: Path):
    """grep and read_file as a session calls them: there a relative path
    resolves inside the session's work area."""
    from durin.agent.tools.context import RequestContext
    from durin.agent.tools.filesystem import ReadFileTool

    ctx = RequestContext(channel="slack", chat_id="C1", session_key="slack:T1")
    grep, read = GrepTool(workspace=workspace), ReadFileTool(workspace=workspace)
    grep.set_context(ctx)
    read.set_context(ctx)
    return grep, read


@pytest.mark.asyncio
async def test_a_long_line_pointer_can_be_followed_from_a_session(tmp_path: Path, run_cap: int) -> None:
    workspace = tmp_path.resolve()
    saved = _one_line_saved_output(workspace)
    grep, read = _session_tools(workspace)

    out = await grep.execute(pattern="THE_FACT_42", path=str(saved), output_mode="content")
    pointer = _LINE_POINTER.search(out)
    assert pointer is not None, out[-400:]
    page = await read.execute(
        path=pointer.group(1), offset=int(pointer.group(2)), limit=1, char_offset=int(pointer.group(3)),
    )

    assert page.startswith("1| "), page[:200]


@pytest.mark.asyncio
async def test_a_match_deep_inside_a_long_line_is_shown_with_a_pointer_to_it(tmp_path: Path, run_cap: int) -> None:
    workspace = tmp_path.resolve()
    saved = _one_line_saved_output(workspace)
    grep, read = _session_tools(workspace)

    out = await grep.execute(pattern="THE_FACT_42", path=str(saved), output_mode="content")

    assert "THE_FACT_42" in out
    pointer = _LINE_POINTER.search(out)
    assert pointer is not None, out[-400:]
    window_start = int(pointer.group(3))
    assert window_start <= _FACT_AT < window_start + 2_000
    page = await read.execute(path=str(saved), offset=1, limit=1, char_offset=window_start)
    assert "THE_FACT_42" in page


@pytest.mark.asyncio
async def test_content_output_fits_the_calling_run_cap(tmp_path: Path) -> None:
    from durin.agent.tools.context import reset_result_char_cap, set_result_char_cap

    (tmp_path / "big.log").write_text(
        "\n".join(f"needle entry {i} " + "pad " * 20 for i in range(500)), encoding="utf-8",
    )
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    token = set_result_char_cap(8_000)
    try:
        result = await tool.execute(pattern="needle", path=".", output_mode="content", head_limit=0)
    finally:
        reset_result_char_cap(token)

    assert len(result) <= 8_000
    assert "use offset=" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [3, 5])
async def test_content_offset_past_the_last_match_says_so(tmp_path: Path, offset: int) -> None:
    (tmp_path / "a.log").write_text("needle 1\nhay\nneedle 2\nneedle 3\n", encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)

    result = await tool.execute(pattern="needle", path=".", output_mode="content", offset=offset)

    assert f"offset {offset} is past the last of 3 matches" in result
    assert "No matches found" not in result


@pytest.mark.asyncio
async def test_a_block_cut_to_fit_keeps_its_matching_line(tmp_path: Path) -> None:
    from durin.agent.tools.context import reset_result_char_cap, set_result_char_cap

    context = [f"context {i} " + "c" * 1_890 for i in range(5)]
    (tmp_path / "wide.log").write_text("\n".join([*context, "THE_MATCH here"]), encoding="utf-8")
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    token = set_result_char_cap(4_000)
    try:
        result = await tool.execute(
            pattern="THE_MATCH", path=".", output_mode="content", context_before=5,
        )
    finally:
        reset_result_char_cap(token)

    assert "> 6| THE_MATCH here" in result
    assert len(result) <= 4_000


@pytest.mark.asyncio
async def test_grep_reports_skipped_binary_and_large_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "large.txt").write_text("x" * 20, encoding="utf-8")

    monkeypatch.setattr(GrepTool, "_MAX_FILE_BYTES", 10)
    # Skip-counters are bookkeeping of the Python walk; under the rg
    # pre-filter those files never reach the loop (rg skips binary/large
    # too). Force the Python path so the counters are exercised.
    monkeypatch.setattr("durin.agent.tools.search.shutil.which", lambda _: None)
    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="needle", path=".")

    assert "No matches found" in result
    assert "skipped 1 binary/unreadable files" in result
    assert "skipped 1 large files" in result


@pytest.mark.asyncio
async def test_search_tools_reject_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-search.txt"
    outside.write_text("secret\n", encoding="utf-8")

    grep_tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)

    grep_result = await grep_tool.execute(pattern="secret", path=str(outside))

    assert grep_result.startswith("Error:")


def test_agent_loop_registers_grep(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    assert "grep" in loop.tools.tool_names


@pytest.mark.asyncio
async def test_subagent_registers_grep(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=4096,
    )
    captured: dict[str, list[str]] = {}

    async def fake_run(spec):
        captured["tool_names"] = spec.tools.tool_names
        return SimpleNamespace(
            stop_reason="ok",
            final_content="done",
            tool_events=[],
            error=None,
        )

    mgr.runner.run = fake_run
    mgr._announce_result = AsyncMock()

    status = SubagentStatus(task_id="sub-1", label="label", task_description="search task", started_at=time.monotonic())
    await mgr._run_subagent("sub-1", "search task", "label", {"channel": "cli", "chat_id": "direct"}, status)

    assert "grep" in captured["tool_names"]


def test_subagent_prompt_respects_disabled_skills(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    skills_dir = tmp_path / "skills"
    (skills_dir / "alpha").mkdir(parents=True)
    (skills_dir / "alpha" / "SKILL.md").write_text("# Alpha\n\nhidden\n", encoding="utf-8")
    (skills_dir / "beta").mkdir(parents=True)
    (skills_dir / "beta" / "SKILL.md").write_text("# Beta\n\nshown\n", encoding="utf-8")

    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=4096,
        disabled_skills=["alpha"],
    )

    prompt = mgr._build_subagent_prompt()

    assert "alpha" not in prompt
    assert "beta" in prompt


# ---------------------------------------------------------------------------
# ripgrep pre-filter (Task 8 — tool-quality-fixes plan)
# ---------------------------------------------------------------------------

import shutil as _shutil

from durin.agent.tools.search import GrepTool as _GrepToolRg

_HAS_RG = _shutil.which("rg") is not None


class TestRipgrepPrefilter:

    @pytest.fixture()
    def tree(self, tmp_path):
        (tmp_path / "a.py").write_text("needle_alpha = 1\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("nothing here\n", encoding="utf-8")
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "c.py").write_text("x = needle_alpha\n", encoding="utf-8")
        return tmp_path

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_RG, reason="ripgrep not installed")
    async def test_rg_and_python_paths_agree(self, tree, monkeypatch):
        tool = _GrepToolRg(workspace=tree)
        rg_result = await tool.execute(
            pattern="needle_alpha", path=str(tree), output_mode="content",
        )
        monkeypatch.setattr("durin.agent.tools.search.shutil.which", lambda _: None)
        py_result = await tool.execute(
            pattern="needle_alpha", path=str(tree), output_mode="content",
        )
        assert rg_result == py_result
        assert "a.py" in rg_result and "c.py" in rg_result

    @pytest.mark.asyncio
    async def test_fallback_when_rg_missing(self, tree, monkeypatch):
        monkeypatch.setattr("durin.agent.tools.search.shutil.which", lambda _: None)
        tool = _GrepToolRg(workspace=tree)
        result = await tool.execute(pattern="needle_alpha", path=str(tree))
        assert "a.py" in result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_RG, reason="ripgrep not installed")
    async def test_python_only_regex_falls_back(self, tree):
        # Lookbehind is unsupported by rg's default engine (exit 2) — the
        # tool must silently fall back to the Python engine and still match.
        tool = _GrepToolRg(workspace=tree)
        result = await tool.execute(
            pattern=r"(?<=needle_)alpha", path=str(tree), output_mode="content",
        )
        assert "a.py" in result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_RG, reason="ripgrep not installed")
    async def test_rg_no_matches(self, tree):
        tool = _GrepToolRg(workspace=tree)
        result = await tool.execute(pattern="zzz_not_there", path=str(tree))
        assert "No matches found" in result
