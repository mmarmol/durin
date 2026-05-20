"""D5.2 layout widget tests.

Verifies the four pieces of chrome (Header / ChatView / InputArea /
FooterBar) mount, render, and respond to a user submission.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import (
    ChatView,
    FooterBar,
    HeaderBar,
    InputArea,
    MessageBubble,
)


@pytest.mark.asyncio
async def test_layout_mounts_four_chrome_widgets() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test():
        for widget_type in (HeaderBar, ChatView, InputArea, FooterBar):
            assert app.query_one(widget_type) is not None


@pytest.mark.asyncio
async def test_input_submission_appends_user_and_assistant_bubbles() -> None:
    """Offline submission opens a user + assistant pair (placeholder body)."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "hola"
        await pilot.press("enter")
        await pilot.pause()
        bubbles = list(chat.query(MessageBubble))
        assert len(bubbles) == 2
        assert bubbles[0]._role == "user"
        assert bubbles[0].body == "hola"
        assert bubbles[1]._role == "assistant"


@pytest.mark.asyncio
async def test_empty_input_does_not_create_bubble() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "   "
        await pilot.press("enter")
        await pilot.pause()
        assert list(chat.query(MessageBubble)) == []


@pytest.mark.asyncio
async def test_message_bubble_append_streams() -> None:
    """ChatView returns the bubble so callers can stream deltas into it."""
    app = DurinApp(agent_loop=None)
    async with app.run_test():
        chat = app.query_one(ChatView)
        bubble = chat.add_message("assistant", "")
        bubble.append("Hel")
        bubble.append("lo, ")
        bubble.append("world!")
        assert bubble.body == "Hello, world!"


@pytest.mark.asyncio
async def test_message_bubble_renders_brackets_literally() -> None:
    """Regression: model output that contains `[...]` (code blocks, checkboxes,
    URLs) must NOT be parsed by Rich as markup — otherwise a malformed tag
    in a streaming delta silently breaks the render and the bubble appears
    empty even though `body` is set."""
    app = DurinApp(agent_loop=None)
    async with app.run_test():
        chat = app.query_one(ChatView)
        # Each of these used to produce a blank or partial render via
        # Rich's markup interpretation. They must all round-trip the body
        # verbatim through the rendered output.
        cases = [
            "[bold]not a tag[/bold]",
            "TODO: [ ] write tests, [x] ship feature",
            "see [this link](https://example.com)",
            "unclosed [bracket and emoji 😄",
            "¡Hola Marcelo! ¿Qué tal?",
        ]
        from rich.console import Console
        from rich.text import Text

        # Plain console (no terminal control codes) so we can read what
        # would end up on screen after Rich's markup pass.
        sink = Console(width=80, force_terminal=False, color_system=None, record=True)
        for raw in cases:
            bubble = chat.add_message("assistant", raw)
            assert bubble.body == raw
            # render() returns a markup string; round-trip it through Rich
            # the same way Textual would, and assert the body text survives.
            sink.print(Text.from_markup(str(bubble.render())))
            output = sink.export_text(clear=True)
            assert raw in output, f"missing body in render output: {output!r}"


@pytest.mark.asyncio
async def test_header_reflects_model_and_workspace(tmp_path) -> None:
    fake_loop = SimpleNamespace(
        workspace=str(tmp_path),
        model="glm-5.1",
        model_preset="default",
    )
    app = DurinApp(agent_loop=fake_loop, cli_chat_id="alpha")
    async with app.run_test():
        # The Static inside HeaderBar reflects workspace + model
        header = app.query_one(HeaderBar)
        assert "glm-5.1" in header.model
        assert str(tmp_path) in header.workspace_path or "~" in header.workspace_path


@pytest.mark.asyncio
async def test_footer_renders_via_payload_getter(tmp_path) -> None:
    fake_loop = SimpleNamespace(
        workspace=str(tmp_path),
        model="glm-5.1",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            get_or_create=lambda key: SimpleNamespace(
                messages=[{"role": "user", "content": "hi"}],
                metadata={},
            )
        ),
    )
    app = DurinApp(agent_loop=fake_loop, cli_chat_id="alpha")
    async with app.run_test() as pilot:
        footer = app.query_one(FooterBar)
        # FooterBar refreshes on mount + on interval; force the refresh now.
        footer.refresh_now()
        await pilot.pause()
        # Rendered Rich markup contains the session key and model.
        assert "cli:alpha" in footer.text
        assert "glm-5.1" in footer.text


@pytest.mark.asyncio
async def test_footer_silent_on_payload_failure() -> None:
    """A getter that raises must not blow up the footer."""

    def _bad():
        raise RuntimeError("boom")

    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        footer = app.query_one(FooterBar)
        footer._payload_getter = _bad  # type: ignore[attr-defined]
        footer.refresh_now()
        await pilot.pause()
        assert footer.text == ""
