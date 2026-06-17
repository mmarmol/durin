"""Tests for second-level slash autocomplete + Tab completion + dropdown."""

from __future__ import annotations

import pytest

from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import CompletionsHint, InputArea, SlashCommandSuggester

# ---------------------------------------------------------------------------
# SlashCommandSuggester — subcommand awareness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_level_command_completion_still_works() -> None:
    s = SlashCommandSuggester()
    assert (await s.get_suggestion("/memo")) == "/memory"
    assert (await s.get_suggestion("/ses")) == "/sessions"


@pytest.mark.asyncio
async def test_memory_subcommand_completion() -> None:
    """`/memory l` should suggest `/memory list`."""
    s = SlashCommandSuggester()
    assert (await s.get_suggestion("/memory l")) == "/memory list"
    assert (await s.get_suggestion("/memory s")) == "/memory show"
    assert (await s.get_suggestion("/memory dri")) == "/memory drill"


@pytest.mark.asyncio
async def test_memory_subcommand_completion_after_space() -> None:
    """`/memory ` (with trailing space, no partial) should suggest the first subcommand."""
    s = SlashCommandSuggester()
    assert (await s.get_suggestion("/memory ")) == "/memory list"


@pytest.mark.asyncio
async def test_subcommand_completion_returns_none_for_unknown_parent() -> None:
    """A slash command without subcommands gets no second-level completion."""
    s = SlashCommandSuggester()
    assert (await s.get_suggestion("/copy a")) is None
    assert (await s.get_suggestion("/help foo")) is None


def test_candidates_top_level_lists_all_matches() -> None:
    s = SlashCommandSuggester()
    cands = s.candidates("/s")
    # Should include several /s-prefixed commands.
    assert "/sessions" in cands
    assert "/status" in cands
    assert "/sources" in cands
    assert "/stop" in cands


def test_candidates_subcommand_lists_all_matches() -> None:
    s = SlashCommandSuggester()
    cands = s.candidates("/memory ")
    assert cands == ["/memory list", "/memory show", "/memory search", "/memory drill", "/memory ingest"]
    # Filtered by prefix.
    cands_s = s.candidates("/memory s")
    assert cands_s == ["/memory show", "/memory search"]


def test_candidates_empty_when_no_slash() -> None:
    s = SlashCommandSuggester()
    assert s.candidates("hola") == []
    assert s.candidates("") == []


# ---------------------------------------------------------------------------
# Tab key completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tab_completes_top_level_command() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/memo"
        await pilot.press("tab")
        await pilot.pause()
        assert inp.value == "/memory"


@pytest.mark.asyncio
async def test_tab_completes_subcommand() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/memory l"
        await pilot.press("tab")
        await pilot.pause()
        assert inp.value == "/memory list"


@pytest.mark.asyncio
async def test_tab_on_empty_value_is_noop() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = ""
        await pilot.press("tab")
        await pilot.pause()
        assert inp.value == ""


# ---------------------------------------------------------------------------
# CompletionsHint live updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completions_hint_appears_for_slash_buffer() -> None:
    """Typing `/me` should populate the hint widget with matching commands."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        hint = app.query_one(CompletionsHint)
        # Initially hidden.
        assert hint.has_class("hidden")

        inp.focus()
        await pilot.pause()
        inp.value = "/me"
        # Wait a few ticks for the Input.Changed → on_input_changed pipeline.
        for _ in range(5):
            await pilot.pause()
        # Hint should now be visible with at least /memory in it.
        assert not hint.has_class("hidden")
        rendered = str(hint.render())
        assert "memory" in rendered


@pytest.mark.asyncio
async def test_completions_hint_hidden_for_plain_text() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        hint = app.query_one(CompletionsHint)
        inp.focus()
        await pilot.pause()
        inp.value = "hola"
        for _ in range(5):
            await pilot.pause()
        assert hint.has_class("hidden")


@pytest.mark.asyncio
async def test_completions_hint_lists_subcommand_options() -> None:
    """Typing `/memory ` should show the memory subcommand menu."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        hint = app.query_one(CompletionsHint)
        inp.focus()
        await pilot.pause()
        inp.value = "/memory "
        for _ in range(5):
            await pilot.pause()
        rendered = str(hint.render())
        assert "list" in rendered
        assert "show" in rendered
        assert "search" in rendered


@pytest.mark.asyncio
async def test_completions_hint_clears_after_submit() -> None:
    """After Enter, the hint should disappear with the cleared input."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        hint = app.query_one(CompletionsHint)
        inp.focus()
        await pilot.pause()
        inp.value = "/me"
        for _ in range(5):
            await pilot.pause()
        assert not hint.has_class("hidden")
        # Submit clears the input → hint should clear too.
        await pilot.press("enter")
        for _ in range(5):
            await pilot.pause()
        assert hint.has_class("hidden")
