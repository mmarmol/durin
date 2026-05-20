"""D5.4 — slash command surface tests.

The TUI dispatches slash commands through the same MessageBus +
CommandRouter the legacy CLI uses, so the bulk of behavior is already
covered by tests/command/test_d1_commands.py. This file verifies:

1. The SlashCommandSuggester wires up the /-prefix autocomplete.
2. Submitting a slash command publishes it onto the bus just like
   any other message (router dispatch happens in the AgentLoop).
"""

from __future__ import annotations

import asyncio

import pytest

from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import InputArea, SlashCommandSuggester


# ---------------------------------------------------------------------------
# SlashCommandSuggester
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggester_matches_known_command() -> None:
    s = SlashCommandSuggester()
    assert (await s.get_suggestion("/ses")) == "/sessions"
    assert (await s.get_suggestion("/comp")) == "/compact"
    assert (await s.get_suggestion("/copy")) is None  # exact match → no suggestion to add


@pytest.mark.asyncio
async def test_suggester_ignores_non_slash() -> None:
    s = SlashCommandSuggester()
    assert (await s.get_suggestion("hola")) is None
    assert (await s.get_suggestion("")) is None
    assert (await s.get_suggestion("/")) is None


@pytest.mark.asyncio
async def test_suggester_case_insensitive() -> None:
    s = SlashCommandSuggester()
    assert (await s.get_suggestion("/SES")) == "/sessions"


@pytest.mark.asyncio
async def test_suggester_returns_none_on_unknown_prefix() -> None:
    s = SlashCommandSuggester()
    assert (await s.get_suggestion("/zzzz")) is None


# ---------------------------------------------------------------------------
# Input plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_has_suggester_by_default() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test():
        inp = app.query_one(InputArea)
        assert isinstance(inp.suggester, SlashCommandSuggester)


@pytest.mark.asyncio
async def test_slash_submit_publishes_inbound_with_slash_intact(tmp_path) -> None:
    """Slash commands flow through the same publish path as any user text."""
    from types import SimpleNamespace

    async def _idle_run() -> None:
        await asyncio.Event().wait()

    bus = MessageBus()
    fake_loop = SimpleNamespace(
        bus=bus,
        workspace=str(tmp_path),
        model="m",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            get_or_create=lambda key: SimpleNamespace(messages=[], metadata={})
        ),
        run=_idle_run,
    )

    received: list[InboundMessage] = []

    async def _drain():
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_inbound(), timeout=0.5)
            except asyncio.TimeoutError:
                return
            received.append(msg)

    app = DurinApp(agent_loop=fake_loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/sessions"
        await pilot.press("enter")
        await pilot.pause()
        await _drain()

    assert any(m.content == "/sessions" for m in received), [m.content for m in received]
