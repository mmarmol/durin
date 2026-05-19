import asyncio
from contextlib import nullcontext
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from prompt_toolkit.formatted_text import HTML

from durin.cli import commands
from durin.cli import stream as stream_mod


@pytest.fixture
def mock_prompt_session():
    """Mock the global prompt session."""
    mock_session = MagicMock()
    mock_session.prompt_async = AsyncMock()
    with patch("durin.cli.commands._PROMPT_SESSION", mock_session), \
         patch("durin.cli.commands.patch_stdout"):
        yield mock_session


@pytest.mark.asyncio
async def test_read_interactive_input_async_returns_input(mock_prompt_session):
    """Test that _read_interactive_input_async returns the user input from prompt_session."""
    mock_prompt_session.prompt_async.return_value = "hello world"

    result = await commands._read_interactive_input_async()
    
    assert result == "hello world"
    mock_prompt_session.prompt_async.assert_called_once()
    args, _ = mock_prompt_session.prompt_async.call_args
    assert isinstance(args[0], HTML)  # Verify HTML prompt is used


@pytest.mark.asyncio
async def test_read_interactive_input_async_handles_eof(mock_prompt_session):
    """Test that EOFError converts to KeyboardInterrupt."""
    mock_prompt_session.prompt_async.side_effect = EOFError()

    with pytest.raises(KeyboardInterrupt):
        await commands._read_interactive_input_async()


def test_init_prompt_session_creates_session():
    """Test that _init_prompt_session initializes the global session."""
    # Ensure global is None before test
    commands._PROMPT_SESSION = None
    
    with patch("durin.cli.commands.PromptSession") as MockSession, \
         patch("durin.cli.commands.FileHistory") as MockHistory, \
         patch("pathlib.Path.home") as mock_home:
        
        mock_home.return_value = MagicMock()
        
        commands._init_prompt_session()
        
        assert commands._PROMPT_SESSION is not None
        MockSession.assert_called_once()
        _, kwargs = MockSession.call_args
        assert kwargs["multiline"] is False
        assert kwargs["enable_open_in_editor"] is False


def test_thinking_spinner_pause_clears_and_restores_indicator():
    """Pause should erase the indicator and rewrite it after the block.

    With the static indicator (no background animation thread), the
    sequence is: enter writes, pause clears, pause exit rewrites, exit
    clears. We track this via the underlying file's writes.
    """
    stream = StringIO()
    stream.isatty = lambda: True  # type: ignore[method-assign]
    from rich.console import Console
    console = Console(file=stream, force_terminal=True)

    thinking = stream_mod.ThinkingSpinner(console=console, bot_name="durin")
    with thinking:
        with thinking.pause():
            pass

    out = stream.getvalue()
    # Two clears (one for pause, one for exit) ⇒ at least two occurrences
    # of the line-clear sequence.
    assert out.count("\r\x1b[2K") >= 2
    # The indicator text was written and then restored after pause.
    assert out.count("durin is thinking") >= 2


@pytest.mark.asyncio
async def test_print_cli_progress_line_pauses_spinner_before_printing():
    """CLI progress output should clear the indicator around the print.

    After the prompt_toolkit coordination fix, ``_print_cli_progress_line``
    is async and routes output through ``run_in_terminal`` →
    ``print_formatted_text``. The new static indicator clears on pause and
    re-writes on exit; we verify both happen around the print.
    """
    order: list[str] = []
    stream = StringIO()
    stream.isatty = lambda: True  # type: ignore[method-assign]
    from rich.console import Console
    console = Console(file=stream, force_terminal=True)

    async def fake_run_in_terminal(write_fn):
        write_fn()

    with patch.object(commands, "run_in_terminal", side_effect=fake_run_in_terminal), \
         patch.object(commands, "print_formatted_text", side_effect=lambda *_a, **_k: order.append("print")):
        thinking = stream_mod.ThinkingSpinner(console=console, bot_name="durin")
        with thinking:
            order.append("entered")
            await commands._print_cli_progress_line("tool running", thinking)
            order.append("after_print")

    # The print happened between the pause clear and the pause-exit
    # rewrite. The exit of the outer `with` then clears again.
    assert "print" in order
    assert order.index("entered") < order.index("print") < order.index("after_print")
    # Indicator was cleared at least twice (pause + exit).
    assert stream.getvalue().count("\r\x1b[2K") >= 2


def test_thinking_spinner_clears_status_line_when_paused():
    """Stopping the indicator should erase its line before yielding."""
    stream = StringIO()
    stream.isatty = lambda: True  # type: ignore[method-assign]
    from rich.console import Console
    console = Console(file=stream, force_terminal=True)

    thinking = stream_mod.ThinkingSpinner(console=console)
    with thinking:
        with thinking.pause():
            pass

    assert "\r\x1b[2K" in stream.getvalue()


def test_stream_renderer_stops_spinner_even_after_header_printed():
    """A later answer delta must clear the indicator even when header already exists."""
    stream = StringIO()
    stream.isatty = lambda: True  # type: ignore[method-assign]
    from rich.console import Console
    console = Console(file=stream, force_terminal=True)

    with patch.object(stream_mod, "_make_console", return_value=console):
        renderer = stream_mod.StreamRenderer(show_spinner=True)
        renderer._header_printed = True
        renderer.ensure_header()

    # The renderer dropped the spinner reference after the stop, and the
    # indicator's line was cleared.
    assert renderer._spinner is None
    assert "\r\x1b[2K" in stream.getvalue()


@pytest.mark.asyncio
async def test_print_cli_progress_line_opens_renderer_header_before_trace():
    """Trace lines should appear under the assistant header, not under You."""
    order: list[str] = []
    renderer = MagicMock()
    renderer.ensure_header.side_effect = lambda: order.append("header")
    renderer.pause_spinner.return_value = nullcontext()

    async def fake_run_in_terminal(write_fn):
        write_fn()

    with patch.object(commands, "run_in_terminal", side_effect=fake_run_in_terminal), \
         patch.object(commands, "print_formatted_text", side_effect=lambda *_a, **_k: order.append("print")):
        await commands._print_cli_progress_line("tool running", None, renderer)

    assert order == ["header", "print"]


@pytest.mark.asyncio
async def test_print_cli_progress_line_stops_live_before_trace():
    """A trace line should not leak the current transient Live frame."""
    mock_live = MagicMock()
    renderer = stream_mod.StreamRenderer(show_spinner=False)
    renderer._live = mock_live

    async def fake_run_in_terminal(write_fn):
        write_fn()

    with patch.object(commands, "run_in_terminal", side_effect=fake_run_in_terminal), \
         patch.object(commands, "print_formatted_text"):
        await commands._print_cli_progress_line("tool running", None, renderer)

    mock_live.stop.assert_called_once()
    assert renderer._live is None


@pytest.mark.asyncio
async def test_print_interactive_progress_line_pauses_spinner_before_printing():
    """Interactive progress output should clear the indicator around the print."""
    order: list[str] = []
    stream = StringIO()
    stream.isatty = lambda: True  # type: ignore[method-assign]
    from rich.console import Console
    console = Console(file=stream, force_terminal=True)

    async def fake_print(_text: str) -> None:
        order.append("print")

    with patch("durin.cli.commands._print_interactive_line", side_effect=fake_print):
        thinking = stream_mod.ThinkingSpinner(console=console)
        with thinking:
            order.append("entered")
            await commands._print_interactive_progress_line("tool running", thinking)
            order.append("after_print")

    assert order == ["entered", "print", "after_print"]
    # Indicator cleared during pause, plus once more on context exit.
    assert stream.getvalue().count("\r\x1b[2K") >= 2


def test_response_renderable_uses_text_for_explicit_plain_rendering():
    status = (
        "🐈 durin v0.1.4.post5\n"
        "🧠 Model: MiniMax-M2.7\n"
        "📊 Tokens: 20639 in / 29 out"
    )

    renderable = commands._response_renderable(
        status,
        render_markdown=True,
        metadata={"render_as": "text"},
    )

    assert renderable.__class__.__name__ == "Text"


def test_response_renderable_preserves_normal_markdown_rendering():
    renderable = commands._response_renderable("**bold**", render_markdown=True)

    assert renderable.__class__.__name__ == "Markdown"


def test_response_renderable_without_metadata_keeps_markdown_path():
    help_text = "🐈 durin commands:\n/status — Show bot status\n/help — Show available commands"

    renderable = commands._response_renderable(help_text, render_markdown=True)

    assert renderable.__class__.__name__ == "Markdown"


def test_stream_renderer_stop_for_input_stops_spinner():
    """stop_for_input should clear the indicator to avoid prompt_toolkit conflicts."""
    stream = StringIO()
    stream.isatty = lambda: True  # type: ignore[method-assign]
    from rich.console import Console
    console = Console(file=stream, force_terminal=True)

    with patch.object(stream_mod, "_make_console", return_value=console):
        renderer = stream_mod.StreamRenderer(show_spinner=True)
        # On creation, the indicator was written exactly once.
        assert "durin is thinking" in stream.getvalue()

        renderer.stop_for_input()

        # Indicator was cleared; the renderer's spinner handle is gone.
        assert renderer._spinner is None
        assert "\r\x1b[2K" in stream.getvalue()


@pytest.mark.asyncio
async def test_on_end_writes_final_content_to_stdout_after_stopping_live():
    """on_end should stop Live (transient erases it) then print final content to stdout."""
    mock_live = MagicMock()
    mock_console = MagicMock()
    mock_console.capture.return_value.__enter__ = MagicMock(
        return_value=MagicMock(get=lambda: "final output\n")
    )
    mock_console.capture.return_value.__exit__ = MagicMock(return_value=False)

    with patch.object(stream_mod, "_make_console", return_value=mock_console):
        renderer = stream_mod.StreamRenderer(show_spinner=False)
        renderer._live = mock_live
        renderer._buf = "final output"

        written: list[str] = []
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.write = lambda s: written.append(s)
            mock_stdout.flush = MagicMock()
            await renderer.on_end()

    mock_live.stop.assert_called_once()
    assert renderer._live is None
    assert written == ["final output\n"]


@pytest.mark.asyncio
async def test_on_end_resuming_clears_buffer_and_restarts_spinner():
    """on_end(resuming=True) should reset buffer and re-create the indicator."""
    stream = StringIO()
    stream.isatty = lambda: True  # type: ignore[method-assign]
    from rich.console import Console
    console = Console(file=stream, force_terminal=True)

    with patch.object(stream_mod, "_make_console", return_value=console):
        renderer = stream_mod.StreamRenderer(show_spinner=True)
        renderer._buf = "some content"

        await renderer.on_end(resuming=True)

    assert renderer._buf == ""
    # The renderer should have a fresh indicator handle ready for the next
    # streaming round (re-created in _start_spinner during resume).
    assert renderer._spinner is not None
    # The indicator text appears twice: once on initial __init__ and once
    # again on the resume restart.
    assert stream.getvalue().count("durin is thinking") >= 2


def test_make_console_force_terminal_when_stdout_is_tty():
    """Console should set force_terminal=True when stdout is a TTY (rich output)."""
    import sys
    with patch.object(sys.stdout, "isatty", return_value=True):
        console = stream_mod._make_console()
        assert console._force_terminal is True


def test_make_console_force_terminal_false_when_stdout_is_not_tty():
    """Console should set force_terminal=False when stdout is not a TTY so that
    ANSI escape codes (cursor visibility, braille spinner frames) don't pollute
    piped output such as `docker exec -i` (#3265)."""
    import sys
    with patch.object(sys.stdout, "isatty", return_value=False):
        console = stream_mod._make_console()
        assert console._force_terminal is False


def test_render_interactive_ansi_force_terminal_follows_isatty():
    """Mirror of _make_console: the capture console used to produce ANSI for
    prompt_toolkit must also defer to sys.stdout.isatty(), otherwise cursor
    escapes and spinner frames leak into piped output (#3265, #3370)."""
    import sys
    captured: dict = {}

    def render_fn(c):
        captured["console"] = c

    with patch.object(sys.stdout, "isatty", return_value=True):
        commands._render_interactive_ansi(render_fn)
        assert captured["console"]._force_terminal is True

    with patch.object(sys.stdout, "isatty", return_value=False):
        commands._render_interactive_ansi(render_fn)
        assert captured["console"]._force_terminal is False
