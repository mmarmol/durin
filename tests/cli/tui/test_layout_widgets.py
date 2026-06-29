"""D5.2 layout widget tests.

Verifies the four pieces of chrome (Header / ChatView / InputArea /
FooterBar) mount, render, and respond to a user submission.
"""

from __future__ import annotations

from types import SimpleNamespace

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
    """Offline submission opens a user + assistant pair (placeholder body).

    The startup logo + info banner are bubbles #0-#1 (roles logo +
    banner); the actual conversation bubbles come after them.
    """
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "hola"
        await pilot.press("enter")
        await pilot.pause()
        # Skip the startup logo + banner — count only conversation bubbles.
        bubbles = [
            b for b in chat.query(MessageBubble)
            if b._role not in ("banner", "logo")
        ]
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
        # Logo + banner stay; nothing else gets added.
        non_banner = [
            b for b in chat.query(MessageBubble)
            if b._role not in ("banner", "logo")
        ]
        assert non_banner == []


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
async def test_assistant_body_renders_through_markdown() -> None:
    """Regression: model output that contains `[...]` patterns (code blocks,
    checkboxes, URLs, unclosed brackets) must land in the rendered output
    verbatim. The original bug was Rich's markup parser swallowing anything
    that looked like a tag; switching the assistant body to Markdown
    rendering sidesteps that path entirely."""
    from rich.console import Console

    app = DurinApp(agent_loop=None)
    async with app.run_test():
        chat = app.query_one(ChatView)
        cases = [
            "[bold]not a tag[/bold]",
            "TODO: [ ] write tests, [x] ship feature",
            "see [this link](https://example.com)",
            "unclosed [bracket and emoji 😄",
            "¡Hola Marcelo! ¿Qué tal?",
            "[INFO] log line",
            "arr[0] = foo",
        ]
        sink = Console(width=80, force_terminal=False, color_system=None, record=True)
        for raw in cases:
            bubble = chat.add_message("assistant", raw)
            assert bubble.body == raw
            # `Static.render()` returns whatever `update()` last set —
            # exactly what Textual routes to its renderer in live mode.
            renderable = bubble._Static__content
            assert renderable is not None, f"no renderable for {raw!r}"
            sink.print(renderable)
            output = sink.export_text(clear=True)
            # Strip Markdown's link decoration (it shows e.g.
            # 'see this link (https://example.com)'). Just check that
            # the meaningful text survived; the exact whitespace and
            # link rendering style is a Markdown formatting concern.
            stripped_raw = raw.replace("[", "").replace("]", "").replace("(", "").replace(")", "")
            stripped_out = output.replace("[", "").replace("]", "").replace("(", "").replace(")", "")
            # Pick a few token markers from the raw text that must appear.
            sentinels: list[str] = []
            for word in raw.split():
                if len(word) >= 3 and word.isalnum() or any(ch in word for ch in "😄¡¿"):
                    sentinels.append(word)
            for sentinel in sentinels[:3]:
                stripped_s = sentinel.replace("[", "").replace("]", "")
                assert stripped_s in stripped_out, (
                    f"missing sentinel {sentinel!r} from {raw!r}: {output!r}"
                )


@pytest.mark.asyncio
async def test_user_body_renders_as_plain_text() -> None:
    """User messages render as Text (no markup interpretation) so the user's
    own input never gets eaten by Rich's parser either."""
    from rich.console import Console

    app = DurinApp(agent_loop=None)
    async with app.run_test():
        chat = app.query_one(ChatView)
        bubble = chat.add_message("user", "tell me about [bracket] arrays")
        sink = Console(width=80, force_terminal=False, color_system=None, record=True)
        sink.print(bubble._Static__content)
        output = sink.export_text(clear=True)
        assert "tell me about" in output
        # Brackets must survive verbatim.
        assert "[bracket]" in output


@pytest.mark.asyncio
async def test_user_vs_assistant_have_distinct_css_class() -> None:
    """The pi-style differentiator is the user box (CSS class), not a label."""
    app = DurinApp(agent_loop=None)
    async with app.run_test():
        chat = app.query_one(ChatView)
        u = chat.add_message("user", "hola")
        a = chat.add_message("assistant", "qué tal")
        assert u.has_class("user")
        assert a.has_class("assistant")
        assert not u.has_class("assistant")
        assert not a.has_class("user")


@pytest.mark.asyncio
async def test_header_reflects_session_label(tmp_path) -> None:
    """Pi-style: the header shows the active session label, not the full path."""
    fake_loop = SimpleNamespace(
        workspace=str(tmp_path),
        model="glm-5.1",
        model_preset="default",
    )
    app = DurinApp(agent_loop=fake_loop, cli_chat_id="alpha")
    async with app.run_test():
        header = app.query_one(HeaderBar)
        assert "alpha" in header.session_label


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


def test_footer_renders_latency_and_mode() -> None:
    from durin.cli.tui.widgets.footer_bar import _render

    out = _render({"model": "opus-4.8", "mode": "build", "latency_ms": 4200})
    assert "build" in out
    assert "4.2s" in out


def test_footer_mode_flanked_by_bullets() -> None:
    from durin.cli.tui.widgets.footer_bar import _render

    out = _render({"model": "opus-4.8", "mode": "build"})
    # Mode is shown as "· build ·" — a bullet on each side, per spec.
    assert "build[/bold] ·" in out


def test_footer_omits_mode_and_latency_when_absent() -> None:
    from durin.cli.tui.widgets.footer_bar import _render

    out = _render({"model": "opus-4.8"})
    assert "⏱" not in out
    # No mode segment: the bold-wrapped mode marker must not appear.
    assert "[bold]" not in out


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


def test_goal_banner_shows_and_hides() -> None:
    from durin.cli.tui.widgets.goal_banner import GoalBanner

    banner = GoalBanner()
    banner.set_goal("ship the work panel", "3/7")
    assert banner.is_shown is True
    assert "ship the work panel" in banner.render_text()
    banner.set_goal(None)
    assert banner.is_shown is False
