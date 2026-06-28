"""Tests for app-level actions: Ctrl+Y copy, spinner lifecycle."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import (
    ChatView,
    MessageBubble,
    SidebarPanel,
    WorkingIndicator,
)


@pytest.mark.asyncio
async def test_ctrl_y_copies_last_assistant_body() -> None:
    """Ctrl+Y must copy the most recent assistant bubble body to clipboard."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        chat.add_message("user", "hola")
        a = chat.add_message("assistant", "")
        a.body = "¡Hola Marcelo! ¿Qué tal?"
        await pilot.pause()
        with patch("durin.utils.clipboard.copy_text") as mock_copy:
            mock_copy.return_value = "pbcopy"
            await pilot.press("ctrl+y")
        mock_copy.assert_called_once()
        assert mock_copy.call_args.args[0] == "¡Hola Marcelo! ¿Qué tal?"


@pytest.mark.asyncio
async def test_ctrl_y_with_no_assistant_message_does_not_copy() -> None:
    """If there's no assistant bubble yet, Ctrl+Y must NOT call the clipboard."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        chat.add_message("user", "hola")  # only user, no assistant
        await pilot.pause()
        with patch("durin.utils.clipboard.copy_text") as mock_copy:
            await pilot.press("ctrl+y")
        mock_copy.assert_not_called()


@pytest.mark.asyncio
async def test_ctrl_y_picks_most_recent_assistant_when_multiple() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        chat.add_message("user", "first")
        a1 = chat.add_message("assistant", "")
        a1.body = "first reply"
        chat.add_message("user", "second")
        a2 = chat.add_message("assistant", "")
        a2.body = "second reply"
        await pilot.pause()
        with patch("durin.utils.clipboard.copy_text") as mock_copy:
            mock_copy.return_value = "pbcopy"
            await pilot.press("ctrl+y")
        assert mock_copy.call_args.args[0] == "second reply"


@pytest.mark.asyncio
async def test_spinner_appears_on_submit_and_disappears_on_first_delta() -> None:
    """The 'thinking…' spinner mounts on submit and unmounts on first content."""
    import asyncio
    from types import SimpleNamespace

    from durin.bus.events import OutboundMessage
    from durin.bus.queue import MessageBus
    from durin.cli.tui.widgets import InputArea

    async def _idle() -> None:
        await asyncio.Event().wait()

    bus = MessageBus()
    fake_loop = SimpleNamespace(
        bus=bus,
        workspace="/tmp/test_workspace",
        model="glm-5.1",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            get_or_create=lambda k: SimpleNamespace(messages=[], metadata={}),
        ),
        run=_idle,
    )
    app = DurinApp(agent_loop=fake_loop, cli_chat_id="spintest")
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "hola"
        await pilot.press("enter")
        await pilot.pause()
        # Spinner must be mounted after submit.
        indicators = list(app.query(WorkingIndicator))
        assert len(indicators) == 1, "spinner did not appear after submit"

        # Simulate first stream delta arriving from the agent.
        await bus.publish_outbound(OutboundMessage(
            channel="cli", chat_id="spintest", content="¡",
            metadata={"_stream_delta": True},
        ))
        # Give the consumer time to process.
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if not list(app.query(WorkingIndicator)):
                break
        indicators = list(app.query(WorkingIndicator))
        assert indicators == [], "spinner did not disappear after first delta"


@pytest.mark.asyncio
async def test_spinner_dismissed_by_reasoning_delta_too() -> None:
    """A reasoning chunk before any content also dismisses the spinner."""
    import asyncio
    from types import SimpleNamespace

    from durin.bus.events import OutboundMessage
    from durin.bus.queue import MessageBus
    from durin.cli.tui.widgets import InputArea

    async def _idle() -> None:
        await asyncio.Event().wait()

    bus = MessageBus()
    fake_loop = SimpleNamespace(
        bus=bus,
        workspace="/tmp/test_workspace",
        model="glm-5.1",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            get_or_create=lambda k: SimpleNamespace(messages=[], metadata={}),
        ),
        run=_idle,
    )
    app = DurinApp(agent_loop=fake_loop, cli_chat_id="reasontest")
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "hola"
        await pilot.press("enter")
        await pilot.pause()
        assert len(list(app.query(WorkingIndicator))) == 1

        await bus.publish_outbound(OutboundMessage(
            channel="cli", chat_id="reasontest", content="thinking…",
            metadata={"_reasoning_delta": True},
        ))
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if not list(app.query(WorkingIndicator)):
                break
        assert list(app.query(WorkingIndicator)) == []


@pytest.mark.asyncio
async def test_spinner_dismissed_by_plain_content() -> None:
    """Slash command results arrive as plain content with no stream flags;
    the spinner must dismiss anyway — otherwise `/memory list` etc. leaves
    a 'thinking…' indicator spinning forever (real bug user reported)."""
    import asyncio
    from types import SimpleNamespace

    from durin.bus.events import OutboundMessage
    from durin.bus.queue import MessageBus
    from durin.cli.tui.widgets import InputArea

    async def _idle() -> None:
        await asyncio.Event().wait()

    bus = MessageBus()
    fake_loop = SimpleNamespace(
        bus=bus, workspace="/tmp/x", model="m", model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(get_or_create=lambda k: SimpleNamespace(messages=[], metadata={})),
        run=_idle,
    )
    app = DurinApp(agent_loop=fake_loop, cli_chat_id="slashtest")
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/memory list"
        await pilot.press("enter")
        await pilot.pause()
        # Spinner should be mounted.
        assert len(list(app.query(WorkingIndicator))) == 1, "spinner missing on submit"

        # Slash response comes through as plain content (router dispatch).
        await bus.publish_outbound(OutboundMessage(
            channel="cli", chat_id="slashtest",
            content="No memory entries found in any class.",
            metadata={"render_as": "text"},
        ))
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if not list(app.query(WorkingIndicator)):
                break
        assert list(app.query(WorkingIndicator)) == [], "spinner stuck after plain content"


@pytest.mark.asyncio
async def test_retry_wait_messages_are_silent() -> None:
    """`_retry_wait` outbound messages must NOT create any bubble."""
    import asyncio
    from types import SimpleNamespace

    from durin.bus.events import OutboundMessage
    from durin.bus.queue import MessageBus
    from durin.cli.tui.widgets import InputArea

    async def _idle() -> None:
        await asyncio.Event().wait()

    bus = MessageBus()
    fake_loop = SimpleNamespace(
        bus=bus, workspace="/tmp/x", model="m", model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(get_or_create=lambda k: SimpleNamespace(messages=[], metadata={})),
        run=_idle,
    )
    app = DurinApp(agent_loop=fake_loop, cli_chat_id="retrytest")
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "hola"
        await pilot.press("enter")
        await pilot.pause()

        chat = app.query_one(ChatView)
        bubbles_before = len(list(chat.query(MessageBubble)))
        await bus.publish_outbound(OutboundMessage(
            channel="cli", chat_id="retrytest",
            content="Model request failed, retry in 1s (attempt 1).",
            metadata={"_retry_wait": True},
        ))
        for _ in range(10):
            await pilot.pause()
            await asyncio.sleep(0.05)
        bubbles_after = len(list(chat.query(MessageBubble)))
        assert bubbles_after == bubbles_before, "retry-wait should not have added a bubble"


@pytest.mark.asyncio
async def test_work_event_opens_sidebar_and_renders():
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        sidebar = app.query_one(SidebarPanel)
        # The sidebar is open by default; if the user hid it, a work event
        # re-opens it (jump_to_work).
        sidebar.hide_sidebar()
        assert sidebar.is_visible is False
        app._route_work_event({
            "name": "workflow_progress", "phase": "running",
            "call_id": "workflow:r1",
            "arguments": {"workflow": "review-changes"},
            "nodes": [{"id": "scan", "label": "scan", "status": "running", "route_label": None}],
        })
        await pilot.pause()
        assert sidebar.is_visible is True
        assert sidebar.has_active_work is True
