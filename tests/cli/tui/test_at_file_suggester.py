"""D5.8 — @file completion tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import (
    AtFileSuggester,
    InputArea,
    MultiModeSuggester,
)


def _make_ws(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "bar_loader.py").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_at_suggester_returns_full_text_with_match(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    s = AtFileSuggester(ws)
    out = await s.get_suggestion("look at @foo")
    assert out == "look at @src/foo.py"


@pytest.mark.asyncio
async def test_at_suggester_substring_match(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    s = AtFileSuggester(ws)
    out = await s.get_suggestion("@loader")
    assert out == "@src/bar_loader.py"


@pytest.mark.asyncio
async def test_at_suggester_skips_excluded_dirs(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    s = AtFileSuggester(ws)
    out = await s.get_suggestion("@config")
    # Only matches non-excluded paths; .git/config must not surface.
    assert out is None or ".git/config" not in out


@pytest.mark.asyncio
async def test_at_suggester_returns_none_on_email_pattern(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    s = AtFileSuggester(ws)
    out = await s.get_suggestion("send mail to foo@bar")
    assert out is None  # `@` is inside a word, not after whitespace


@pytest.mark.asyncio
async def test_at_suggester_returns_none_without_prefix(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    s = AtFileSuggester(ws)
    assert (await s.get_suggestion("see @")) is None


@pytest.mark.asyncio
async def test_at_suggester_no_at_in_text(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    s = AtFileSuggester(ws)
    assert (await s.get_suggestion("just plain text")) is None


@pytest.mark.asyncio
async def test_multimode_routes_slash_and_at(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    s = MultiModeSuggester(workspace=ws)
    assert (await s.get_suggestion("/ses")) == "/sessions"
    assert (await s.get_suggestion("@foo")) == "@src/foo.py"
    assert (await s.get_suggestion("hola")) is None


@pytest.mark.asyncio
async def test_input_uses_multimode_when_workspace_provided(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    inp = InputArea(workspace=ws)
    assert isinstance(inp.suggester, MultiModeSuggester)


@pytest.mark.asyncio
async def test_app_input_uses_multimode_when_agent_loop_present(tmp_path: Path) -> None:
    from types import SimpleNamespace

    ws = _make_ws(tmp_path)
    fake_loop = SimpleNamespace(
        bus=None,
        workspace=str(ws),
        model="m",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            get_or_create=lambda key: SimpleNamespace(messages=[], metadata={})
        ),
    )
    app = DurinApp(agent_loop=fake_loop)
    async with app.run_test():
        inp = app.query_one(InputArea)
        assert isinstance(inp.suggester, MultiModeSuggester)
