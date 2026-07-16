"""CLI commands for durin."""

import asyncio
import os
import select
import signal
import sys
from collections.abc import Callable
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        with suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import typer
from loguru import logger

# Remove default handler and re-add with unified durin format
logger.remove()
_log_handler_id = logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <5}</level> | "
        "<cyan>{extra[channel]}</cyan> | "
        "<level>{message}</level>"
    ),
    level="INFO",
    colorize=None,
    filter=lambda record: record["extra"].setdefault("channel", "-") or True,
)

# Route durin's own stdlib loggers (durin.*) into loguru so their records
# reach every loguru sink — the stderr sink above and, in daemon mode, the
# gateway.log JSONL file sink. Without this, modules that log via
# logging.getLogger(__name__) bypass gateway.log entirely.
from durin.utils.logging_bridge import redirect_durin_logging

redirect_durin_logging()

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from durin import __logo__, __version__
from durin.agent.loop import AgentLoop


def _sanitize_surrogates(text: str) -> str:
    """Reconstruct surrogate pairs into real characters; replace lone surrogates.

    On Windows, console input may produce lone surrogate code points (e.g.
    ``\\ud83d\\udc08`` for U+1F408).  Round-tripping through UTF-16 reconstructs
    paired surrogates into their actual characters and replaces unpaired ones
    with U+FFFD.
    """
    return text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")


class SafeFileHistory(FileHistory):
    """FileHistory subclass that sanitizes surrogate characters on write.

    On Windows, special Unicode input (emoji, mixed-script) can produce
    surrogate characters that crash prompt_toolkit's file write.
    See issue #2846.
    """

    def store_string(self, string: str) -> None:
        super().store_string(_sanitize_surrogates(string))
from durin.agent.agent_mode import register_config_modes
from durin.cli.stream import StreamRenderer, ThinkingSpinner
from durin.config.paths import get_workspace_path, is_default_workspace
from durin.config.schema import Config
from durin.personas import seed_example_personas
from durin.utils.helpers import sync_workspace_templates
from durin.utils.restart import (
    consume_restart_notice_from_env,
    format_restart_completed_message,
    should_show_cli_restart_notice,
)

# Shown at the bottom of `durin --help`. The top-level listing only
# gives one line per command and never reveals what lives *inside* the
# command groups (`config`, `gateway`, …) — so spell those out here.
_HELP_FOOTER = (
    "First run: `durin onboard`  ·  Health check: `durin doctor`  ·  "
    "Chat: `durin agent`"
)
# Static fallback; the real epilog is regenerated from the registered
# groups by `_refresh_help_epilog()` at the end of this module so it can
# never go stale when a group or subcommand is added.
_HELP_EPILOG = (
    "Command groups (run `durin GROUP --help` for the full list):\n\n"
    "[bold]config[/bold] — path, show, get, set, edit, import\n\n"
    "[bold]secret[/bold] — set, list, show, rm, grant, revoke, migrate\n\n"
    "[bold]gateway[/bold] — start, stop, restart, status, logs\n\n"
    "[bold]channels[/bold] — status, login\n\n"
    "[bold]oauth[/bold] — login, logout\n\n"
    + _HELP_FOOTER
)

app = typer.Typer(
    name="durin",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        f"{__logo__} durin — your personal AI agent.\n\n"
        "Daily-driver CLI and modal TUI, an OpenAI-compatible API server, "
        "and a gateway that bridges chat channels (Telegram / Slack / "
        "Discord) with cron jobs and a web dashboard."
    ),
    epilog=_HELP_EPILOG,
    no_args_is_help=True,
    # Hide the auto-injected `--install-completion` / `--show-completion`
    # flags. They're Typer boilerplate, not durin functionality —
    # power users who want tab-completion can still set it up manually
    # via their shell (see docs/guide/install.md).
    add_completion=False,
)

# D6 lifecycle commands: config get/set/show/edit/path, upgrade, uninstall.
# Registered at the end of the module (see bottom of file) so the
# everyday commands (onboard, agent, gateway, …) sort above the
# lifecycle/diagnostic ones in `durin --help`.
from durin.cli.config_cmd import config_app as _config_app  # noqa: E402
from durin.cli.doctor import register as _register_doctor  # noqa: E402
from durin.cli.uninstall import register as _register_uninstall  # noqa: E402
from durin.cli.upgrade import register as _register_upgrade  # noqa: E402

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    with suppress(Exception):
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        return

    with suppress(Exception):
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    with suppress(Exception):
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)


def _init_prompt_session(
    workspace: Path | None = None,
    presets_getter=None,
    footer_getter=None,
) -> None:
    """Create the prompt_toolkit session with persistent file history.

    ``workspace`` enables the ``@file`` completer (D1.9).
    ``presets_getter`` enables the ``/model <preset>`` completer (D1.7)
    and the Ctrl+L shortcut that pre-fills ``/model ``.
    ``footer_getter`` enables the persistent footer (D1.6) — a callable
    returning a prompt_toolkit-renderable that is evaluated on each redraw.
    """
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    with suppress(Exception):
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())

    from durin.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    from prompt_toolkit.completion import merge_completers
    from prompt_toolkit.document import Document as _PtkDocument
    from prompt_toolkit.key_binding import KeyBindings

    completers = []
    if workspace is not None:
        from durin.cli.completers import FileReferenceCompleter

        completers.append(FileReferenceCompleter(workspace))
    if presets_getter is not None:
        from durin.cli.completers import ModelPresetCompleter

        completers.append(ModelPresetCompleter(presets_getter))
    completer = merge_completers(completers) if completers else None

    kb = KeyBindings()

    @kb.add("c-l")
    def _open_model_picker(event):
        """Ctrl+L: replace current input with `/model ` to start picker flow."""
        buf = event.app.current_buffer
        buf.document = _PtkDocument("/model ", cursor_position=len("/model "))

    _PROMPT_SESSION = PromptSession(
        history=SafeFileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
        completer=completer,
        complete_while_typing=True,
        key_bindings=kb,
        bottom_toolbar=footer_getter,
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=sys.stdout.isatty(),
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
    show_header: bool = True,
) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata)
    if show_header:
        console.print()
        console.print(f"[cyan]{__logo__} durin[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """Render plain-text command output without markdown collapsing newlines."""
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} durin[/cyan]"),
                c.print(_response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_cli_progress_line(
    text: str,
    thinking: ThinkingSpinner | None,
    renderer: StreamRenderer | None = None,
) -> None:
    """Print a CLI progress line. Uses ``run_in_terminal`` + ANSI capture so
    output is coordinated with prompt_toolkit (no input/output clobbering)."""
    if not text.strip():
        return
    pause = renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())

    def _write() -> None:
        with pause:
            if renderer:
                renderer.ensure_header()
            ansi = _render_interactive_ansi(
                lambda c: c.print(f"  [dim]↳ {text}[/dim]")
            )
            print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


# Reasoning chunks arrive token-by-token via the streaming progress hook.
# Without buffering we'd render every chunk on its own line prefixed with
# `✻`, producing a vertical stream of two-word lines. Buffer chunks and
# emit one styled line per logical newline; flush whatever remains when
# the reasoning segment ends (signalled by `reasoning_end=True`).
#
# All emission goes through `run_in_terminal` + `print_formatted_text` so
# the output coordinates correctly with prompt_toolkit's input line —
# without this, the streaming output and the "You: " prompt get
# interleaved on the same terminal row.
_reasoning_buf: list[str] = []


async def _emit_reasoning_line(
    text: str,
    thinking: ThinkingSpinner | None,
    renderer: StreamRenderer | None = None,
) -> None:
    """Render one complete reasoning line in the dim-italic ✻ style."""
    if not text.strip():
        return
    pause = renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())

    def _write() -> None:
        with pause:
            if renderer:
                renderer.ensure_header()
            ansi = _render_interactive_ansi(
                lambda c: c.print(f"[dim italic]✻ {text}[/dim italic]")
            )
            print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_cli_reasoning(
    text: str,
    thinking: ThinkingSpinner | None,
    renderer: StreamRenderer | None = None,
) -> None:
    """Append a reasoning chunk to the buffer; emit each complete line."""
    if not text:
        return
    _reasoning_buf.append(text)
    buffered = "".join(_reasoning_buf)
    if "\n" not in buffered:
        return
    lines = buffered.split("\n")
    # Emit every fully-terminated line; keep the trailing (possibly partial) one
    for line in lines[:-1]:
        await _emit_reasoning_line(line, thinking, renderer)
    _reasoning_buf[:] = [lines[-1]] if lines[-1] else []


async def _flush_cli_reasoning(
    thinking: ThinkingSpinner | None,
    renderer: StreamRenderer | None = None,
) -> None:
    """Emit any remaining buffered reasoning text. Called on reasoning_end."""
    text = "".join(_reasoning_buf).strip()
    _reasoning_buf.clear()
    if text:
        await _emit_reasoning_line(text, thinking, renderer)


async def _print_interactive_progress_line(text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    if not text.strip():
        return
    if renderer:
        with renderer.pause_spinner():
            renderer.ensure_header()
            renderer.console.print(f"  [dim]↳ {text}[/dim]")
    else:
        with thinking.pause() if thinking else nullcontext():
            await _print_interactive_line(text)


async def _maybe_print_interactive_progress(
    msg: Any,
    thinking: ThinkingSpinner | None,
    channels_config: Any,
    renderer: StreamRenderer | None = None,
) -> bool:
    metadata = msg.metadata or {}
    if metadata.get("_retry_wait"):
        await _print_interactive_progress_line(msg.content, thinking, renderer)
        return True

    if not metadata.get("_progress"):
        return False

    agent_ui = metadata.get("_agent_ui")
    if agent_ui:
        from durin.cli.agent_ui_render import render_agent_ui
        target = renderer.console if renderer else console
        pause = renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())
        with pause:
            if renderer:
                renderer.ensure_header()
            render_agent_ui(target, agent_ui)
        return True

    is_tool_hint = metadata.get("_tool_hint", False)
    is_reasoning_end = metadata.get("_reasoning_end", False)
    is_reasoning = metadata.get("_reasoning", False) or metadata.get("_reasoning_delta", False)
    if is_reasoning_end:
        if channels_config and not channels_config.show_reasoning:
            return True
        await _flush_cli_reasoning(thinking, renderer)
        return True
    if is_reasoning:
        if channels_config and not channels_config.show_reasoning:
            return True
        await _print_cli_reasoning(msg.content, thinking, renderer)
        return True
    if is_tool_hint:
        # Interactive payloads (question/secret/plan) must reach the user
        # even when text tool-hints are disabled — the model no longer
        # re-presents them in prose (durin/agent/user_payloads.py).
        from durin.agent.user_payloads import format_interactive_tool_event

        printed_interaction = False
        for event in metadata.get("_tool_events") or []:
            text = format_interactive_tool_event(event)
            if text:
                await _print_interactive_progress_line(text, thinking, renderer)
                printed_interaction = True
        if printed_interaction:
            return True
    if channels_config and is_tool_hint and not channels_config.send_tool_hints:
        return True
    if channels_config and not is_tool_hint and not channels_config.send_progress:
        return True

    await _print_interactive_progress_line(msg.content, thinking, renderer)
    return True


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} durin v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """durin - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


def _stdin_is_interactive() -> bool:
    """True when stdin is a usable interactive TTY.

    The wizard's prompts (questionary) need a real terminal. Factored
    out as a module function so tests of the interactive path can
    monkeypatch it instead of fighting the test runner's piped stdin.
    """
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


@app.command()
def onboard(
    section: str | None = typer.Argument(
        None,
        help="Jump straight to one section: model, vision, audio, memory, "
        "web, dashboard, channels, workspace.",
    ),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    no_wizard: bool = typer.Option(
        False, "--no-wizard",
        help="Skip the interactive wizard; write defaults silently.",
    ),
    advanced: bool = typer.Option(
        False, "--advanced",
        help="Use the legacy field-by-field walker instead of the task-oriented wizard.",
    ),
):
    """Initialize durin configuration and workspace.

    Runs the task-oriented wizard by default (provider/key/model + an
    opt-in menu of features). Pass a SECTION to re-tune just one thing
    (e.g. `durin onboard model`). Use `--no-wizard` to just write
    defaults or `--advanced` for the legacy walk-every-config-field mode.
    """
    from durin.config.loader import (
        backup_config,
        get_config_path,
        load_config,
        save_config,
        set_config_path,
    )
    from durin.config.schema import Config

    if config:
        config_path = Path(config).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # Default mode: task-oriented wizard. Legacy field-walker available
    # via --advanced; silent defaults via --no-wizard.
    wizard_mode = not no_wizard

    # A section ("durin onboard model") must be valid and is wizard-only.
    if section is not None:
        from durin.cli.onboard_wizard import SECTIONS

        if section not in SECTIONS:
            console.print(f"[red]✗[/red] Unknown section '{section}'.")
            console.print(f"[dim]Valid sections: {', '.join(SECTIONS)}[/dim]")
            raise typer.Exit(1)
        if no_wizard or advanced:
            console.print(
                "[red]✗[/red] A section can't be combined with "
                "--no-wizard / --advanced."
            )
            raise typer.Exit(1)

    # Non-interactive guard: the wizard needs a TTY for questionary.
    # Without one, write defaults (if missing) and show how to configure
    # via `durin config set` instead of crashing on a blind prompt.
    if wizard_mode and not _stdin_is_interactive():
        console.print(
            "[yellow]No interactive terminal detected — skipping the wizard.[/yellow]"
        )
        if not config_path.exists():
            save_config(_apply_workspace_override(Config()), config_path)
            console.print(f"[green]✓[/green] Wrote default config to {config_path}")
        console.print("[dim]Configure non-interactively, e.g.:[/dim]")
        console.print("  [cyan]durin config set agents.defaults.provider zhipu[/cyan]")
        console.print("  [cyan]durin config set providers.zhipu.apiKey sk-...[/cyan]")
        console.print("  [cyan]durin config set agents.defaults.model glm-5.1[/cyan]")
        console.print("[dim]Or re-run `durin onboard` in an interactive shell.[/dim]")
        return

    # Load base config: existing file if present, fresh defaults otherwise.
    if config_path.exists():
        config = _apply_workspace_override(load_config(config_path))
        if not wizard_mode:
            console.print(f"[yellow]Config exists at {config_path}; nothing to do.[/yellow]")
            console.print("[dim]Re-run without --no-wizard to update interactively.[/dim]")
            return
    else:
        config = _apply_workspace_override(Config())
        if not wizard_mode:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    if wizard_mode and advanced:
        # Legacy walker — exhaustive, walks every Pydantic field.
        from durin.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return
            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'durin onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1) from None
    elif wizard_mode:
        # New default: task-oriented wizard. A section runs just one part.
        from durin.cli.onboard_wizard import run_section, run_wizard

        # Snapshot the existing config so a botched re-run can be reverted.
        backup = backup_config(config_path) if config_path.exists() else None

        try:
            if section is not None:
                result = run_section(config, section)
            else:
                result = run_wizard(config)
        except RuntimeError as e:
            console.print(f"[red]✗[/red] {e}")
            console.print(
                "[yellow]Tip: re-run with `durin onboard --advanced` "
                "for the legacy walker.[/yellow]"
            )
            raise typer.Exit(1) from None
        if result.cancelled:
            console.print("[yellow]Wizard cancelled; nothing was saved.[/yellow]")
            return
        config = result.config
        save_config(config, config_path)
        console.print(f"[green]✓[/green] Config saved at {config_path}")
        if backup is not None:
            console.print(f"[dim]Previous config backed up to {backup}[/dim]")
        # Surface the summary + the install command for missing extras.
        if result.summary_lines:
            console.print("\n[bold]Configured:[/bold]")
            for line in result.summary_lines:
                console.print(f"  • {line}")
        if result.extras_to_install:
            # The wizard chose features (memory / web / …) that need
            # extra Python packages. Detect which are actually missing
            # and offer to install them now instead of leaving the user
            # a command to copy-paste.
            from durin.cli.doctor import detect_installed_extras, install_missing_extras
            from durin.cli.upgrade import install_hint

            already = set(detect_installed_extras())
            missing = sorted(set(result.extras_to_install) - already)
            if not missing:
                console.print(
                    "\n[green]✓[/green] All chosen extras are already installed."
                )
            else:
                console.print(
                    f"\n[bold]These features need extra packages:[/bold] "
                    f"{', '.join(missing)}"
                )
                if typer.confirm("Install them now?", default=True):
                    rc = install_missing_extras(missing, assume_yes=True)
                    if rc == 0:
                        console.print(
                            "[green]✓[/green] Extras installed — "
                            "restart durin to pick them up."
                        )
                    else:
                        console.print(
                            "[yellow]Install did not complete cleanly. "
                            "Run it manually:[/yellow]"
                        )
                        console.print(f"  [cyan]{install_hint(missing)}[/cyan]")
                else:
                    console.print("[dim]Skipped. Install later with:[/dim]")
                    console.print(f"  [cyan]{install_hint(missing)}[/cyan]")
        # Capability matrix: what works now, and the exact command to
        # fill each gap.
        if result.availability_lines:
            console.print("\n[bold]Capabilities:[/bold]")
            for line in result.availability_lines:
                style = "green" if line.startswith("✓") else "dim"
                console.print(f"  [{style}]{line}[/{style}]")
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    agent_cmd = 'durin agent -m "Hello!"'
    gateway_cmd = "durin gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} durin is ready!")
    console.print("\nNext steps:")
    if wizard_mode:
        console.print("  1. Verify: [cyan]durin doctor[/cyan]")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  3. (optional) Chat apps: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print("  1. Configure a provider — either run the wizard:")
        console.print("       [cyan]durin onboard[/cyan]")
        console.print("     or set it from the command line:")
        console.print(
            "       [cyan]durin secret set ZHIPU_API_KEY --service provider:zhipu[/cyan]"
        )
        console.print(
            "       [cyan]durin config set providers.zhipu.apiKey "
            "'${secret:ZHIPU_API_KEY}'[/cyan]"
        )
        console.print(
            "       [cyan]durin config set agents.defaults.provider zhipu[/cyan]"
        )
        console.print("  2. Verify: [cyan]durin doctor[/cyan]")
        console.print(f"  3. Chat: [cyan]{agent_cmd}[/cyan]")


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Backfill the full attribute set for **enabled** channels only.

    Previously this eagerly injected every discovered channel's
    ``default_config()`` — which buried the config under a dozen
    disabled channels' worth of noise. The user's rule: unconfigured
    channels stay absent; an *enabled* channel gets its full attribute
    set so every editable field is visible.

    Layout-aware: reads/writes via the shared loader helpers so the
    split-file layout and the legacy monolith both work.
    """
    from durin.channels.registry import discover_all
    from durin.config.loader import (
        _is_split_layout,
        _prune_noise_sections,
        _write_split_layout,
        read_persisted_config,
    )

    all_channels = discover_all()
    if not all_channels:
        return

    data = read_persisted_config(config_path)
    channels = data.get("channels")
    if isinstance(channels, dict):
        for name, cls in all_channels.items():
            section = channels.get(name)
            # Only touch channels the user has already configured AND
            # enabled — backfill any missing attributes so the section
            # is complete + editable. Disabled / absent channels are
            # left alone (no noise).
            if isinstance(section, dict) and section.get("enabled"):
                channels[name] = _merge_missing_defaults(section, cls.default_config())

    # Strip any leftover all-default disabled channels / empty providers
    # so an existing noisy config gets cleaned up on this pass too.
    data = _prune_noise_sections(data)

    if _is_split_layout(config_path):
        _write_split_layout(data, config_path)
    else:
        import json

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def _model_display(config: Config) -> tuple[str, str]:
    """Return (resolved_model_name, preset_tag) for display strings."""
    resolved = config.resolve_preset()
    name = config.agents.defaults.model_preset
    tag = f" (preset: {name})" if name else ""
    return resolved.model, tag


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from durin.config.loader import load_config, resolve_config_env_vars, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    try:
        loaded = resolve_config_env_vars(load_config(config_path))
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json

    from durin.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )


def _migrate_cron_store(config: "Config") -> None:
    """One-time migration: move legacy global cron store into the workspace."""
    from durin.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_path), str(new_path))


# ============================================================================
# OpenAI-Compatible API Server
# ============================================================================


@app.command()
def serve(
    port: int | None = typer.Option(None, "--port", "-p", help="API server port"),
    host: str | None = typer.Option(None, "--host", "-H", help="Bind address"),
    timeout: float | None = typer.Option(None, "--timeout", "-t", help="Per-request timeout (seconds)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show durin runtime logs"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the OpenAI-compatible API server (/v1/chat/completions)."""
    try:
        from aiohttp import web  # noqa: F401
    except ImportError:
        console.print("[red]aiohttp is required. Install with: pip install 'durin-ai[api]'[/red]")
        raise typer.Exit(1) from None

    from loguru import logger

    from durin.api.server import create_app
    from durin.bus.queue import MessageBus
    from durin.session.manager import SessionManager

    if verbose:
        logger.enable("durin")
    else:
        logger.disable("durin")

    runtime_config = _load_runtime_config(config, workspace)
    api_cfg = runtime_config.api
    host = host if host is not None else api_cfg.host
    port = port if port is not None else api_cfg.port
    timeout = timeout if timeout is not None else api_cfg.timeout
    sync_workspace_templates(runtime_config.workspace_path)
    seed_example_personas()  # one-time: example personas into config (marker-guarded)
    register_config_modes(runtime_config.agent_modes)  # custom modes → agent-mode registry
    bus = MessageBus()
    session_manager = SessionManager(runtime_config.workspace_path)
    try:
        agent_loop = AgentLoop.from_config(
            runtime_config, bus,
            session_manager=session_manager,
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc

    model_name, preset_tag = _model_display(runtime_config)
    console.print(f"{__logo__} Starting OpenAI-compatible API server")
    console.print(f"  [cyan]Endpoint[/cyan] : http://{host}:{port}/v1/chat/completions")
    console.print(f"  [cyan]Model[/cyan]    : {model_name}{preset_tag}")
    console.print("  [cyan]Session[/cyan]  : api:default")
    console.print(f"  [cyan]Timeout[/cyan]  : {timeout}s")
    if host in {"0.0.0.0", "::"}:
        console.print(
            "[yellow]Warning:[/yellow] API is bound to all interfaces. "
            "Only do this behind a trusted network boundary, firewall, or reverse proxy."
        )
    console.print()

    api_app = create_app(agent_loop, model_name=model_name, request_timeout=timeout)

    async def on_startup(_app):
        await agent_loop._connect_mcp()

    async def on_cleanup(_app):
        await agent_loop.close_mcp()

    api_app.on_startup.append(on_startup)
    api_app.on_cleanup.append(on_cleanup)

    web.run_app(api_app, host=host, port=port, print=lambda msg: logger.info(msg))


# ============================================================================
# Gateway / Server
# ============================================================================


gateway_app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Run the durin gateway (webui + channels + cron).",
)
app.add_typer(gateway_app, name="gateway")


def _gateway_apply_verbose() -> None:
    """Switch the logger to DEBUG when --verbose is requested."""
    logger.remove(_log_handler_id)
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <5}</level> | "
            "<cyan>{extra[channel]}</cyan> | "
            "<level>{message}</level>"
        ),
        level="DEBUG",
        colorize=None,
        filter=lambda record: record["extra"].setdefault("channel", "-") or True,
    )


@gateway_app.callback()
def gateway_root(
    ctx: typer.Context,
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    foreground: bool = typer.Option(
        False, "--foreground",
        help="Force foreground mode even if config.gateway.daemon is true. "
             "Used internally by `gateway start` to launch the detached child.",
    ),
):
    """Run the gateway.

    With no subcommand, follows ``config.gateway.daemon``:
    - ``daemon=false`` (default): runs foreground, terminal locked.
    - ``daemon=true``: detaches into the background (same as `gateway start`).

    Use ``--foreground`` to force foreground regardless of config.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand (start / stop / status / …) will handle the run.
        # Stash shared opts on ctx so subcommands can read them.
        ctx.obj = {
            "port": port, "workspace": workspace, "verbose": verbose,
            "config": config, "foreground": foreground,
        }
        return

    if verbose:
        _gateway_apply_verbose()
    cfg = _load_runtime_config(config, workspace)
    # Respect config.gateway.daemon unless --foreground forces inline.
    if cfg.gateway.daemon and not foreground:
        from durin.cli.gateway_daemon import AlreadyRunningError, start_daemon

        try:
            pid = start_daemon([])
        except AlreadyRunningError as e:
            console.print(
                f"[yellow]Gateway already running (pid {e.pid}). "
                "Use `durin gateway stop` first if you want a fresh start.[/yellow]"
            )
            raise typer.Exit(1) from None
        from durin.cli.gateway_daemon import daemon_logs_path

        console.print(f"[green]✓[/green] Gateway started (pid {pid}, daemon mode)")
        url = _resolved_webui_url()
        if url:
            console.print(f"  Dashboard: [cyan]{url}[/cyan]")
        console.print(f"  Logs:      [cyan]{daemon_logs_path()}[/cyan]")
        console.print("  Stop:      [cyan]durin gateway stop[/cyan]")
        return
    _run_gateway(cfg, port=port)


def _resolved_webui_url() -> str | None:
    """Return the URL where the webui dashboard would be served, if enabled."""
    try:
        from durin.config.loader import load_config

        cfg = load_config()
    except Exception:  # noqa: BLE001
        return None
    if not getattr(cfg.gateway, "webui_enabled", False):
        return None
    from durin.utils.public_url import dashboard_url

    return dashboard_url(cfg) + "/"


@gateway_app.command("start")
def gateway_start(ctx: typer.Context) -> None:
    """Detach the gateway into the background regardless of config.gateway.daemon."""
    from durin.cli.gateway_daemon import AlreadyRunningError, daemon_logs_path, start_daemon

    opts = ctx.obj or {}
    extra: list[str] = []
    if opts.get("port") is not None:
        extra += ["--port", str(opts["port"])]
    if opts.get("workspace"):
        extra += ["--workspace", str(opts["workspace"])]
    if opts.get("verbose"):
        extra.append("--verbose")
    if opts.get("config"):
        extra += ["--config", str(opts["config"])]

    try:
        pid = start_daemon(extra)
    except AlreadyRunningError as e:
        console.print(
            f"[yellow]Gateway already running (pid {e.pid}). "
            "Run `durin gateway restart` to bounce it.[/yellow]"
        )
        raise typer.Exit(1) from None
    console.print(f"[green]✓[/green] Gateway started in background (pid {pid})")
    url = _resolved_webui_url()
    if url:
        console.print(f"  Dashboard: [cyan]{url}[/cyan]")
    console.print(f"  Logs:      [cyan]{daemon_logs_path()}[/cyan]")


@gateway_app.command("stop")
def gateway_stop() -> None:
    """Stop the background gateway daemon."""
    from durin.cli.gateway_daemon import daemon_status, stop_daemon

    before = daemon_status()
    if before.state == "not_running":
        console.print("[dim]Gateway is not running.[/dim]")
        return
    if before.state == "stale_pid":
        console.print(
            f"[yellow]Stale PID file at {before.pid_file} (process is gone). "
            "Cleaning up.[/yellow]"
        )
        stop_daemon()
        return
    after = stop_daemon()
    if after.state == "not_running":
        console.print(f"[green]✓[/green] Gateway stopped (was pid {before.pid})")
    else:
        console.print(
            f"[red]✗[/red] Gateway did not stop cleanly; state: {after.state}"
        )
        raise typer.Exit(1)


@gateway_app.command("restart")
def gateway_restart(ctx: typer.Context) -> None:
    """Stop and start the gateway daemon."""
    gateway_stop()
    gateway_start(ctx)


@gateway_app.command("status")
def gateway_status_cmd() -> None:
    """Show whether the gateway daemon is running."""
    from durin.cli.gateway_daemon import daemon_status

    s = daemon_status()
    if s.state == "running":
        console.print(f"[green]✓[/green] Gateway is running (pid {s.pid})")
        url = _resolved_webui_url()
        if url:
            console.print(f"  Dashboard: [cyan]{url}[/cyan]")
        console.print(f"  Logs:      [cyan]{s.log_file}[/cyan]")
    elif s.state == "stale_pid":
        console.print(
            f"[yellow]Stale PID file at {s.pid_file} — process is gone.[/yellow]"
        )
        console.print("  Run `durin gateway start` to relaunch.")
    else:
        console.print("[dim]Gateway is not running.[/dim]")


@gateway_app.command("logs")
def gateway_logs_cmd(
    follow: bool = typer.Option(False, "--follow", "-f", help="tail -f mode"),
    lines: int = typer.Option(80, "--lines", "-n", help="How many trailing lines to show"),
) -> None:
    """Show the gateway daemon's log output."""
    from durin.cli.gateway_daemon import daemon_logs_path

    log_path = daemon_logs_path()
    if not log_path.exists():
        console.print(f"[dim]No log file yet at {log_path}.[/dim]")
        return
    import subprocess as _sub

    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(log_path))
    try:
        _sub.run(cmd, check=False)
    except KeyboardInterrupt:
        pass


def _run_gateway(
    config: Config,
    *,
    port: int | None = None,
    open_browser_url: str | None = None,
) -> None:
    """Shared gateway runtime; ``open_browser_url`` opens a tab once channels are up."""
    # Attach the JSONL rotating/compressing file sink to gateway.log for
    # EVERY gateway run — daemon and foreground alike — so the dashboard log
    # viewer has structured input regardless of how the process is
    # supervised (durin's own daemon, systemd with --foreground, a terminal).
    # The human-readable stderr sink stays as-is: under systemd that is what
    # journald captures; in a terminal it is what the operator sees.
    from durin.cli.gateway_daemon import daemon_logs_path
    from durin.cli.gateway_logging import (
        configure_gateway_file_logging,
        install_excepthook,
    )

    configure_gateway_file_logging(
        daemon_logs_path(),
        max_file_mb=config.logging.max_file_mb,
        retention_days=config.logging.retention_days,
    )
    install_excepthook()
    # When the webui is requested via config, ensure the websocket
    # channel (which serves the SPA static files + the WS endpoint) is
    # turned on at RUNTIME — without mutating the persisted config.
    # The user picked `webui_enabled=true` because they want the
    # dashboard, not because they want to think about which channel
    # serves it. We *also* disable the token-required handshake by
    # default in this auto-enable path so the SPA loads in the browser
    # without needing the user to set up auth first.
    if getattr(config.gateway, "webui_enabled", False):
        ws_section = getattr(config.channels, "websocket", None)
        if ws_section is None:
            # No `channels.websocket` section in the user's config — add
            # one in-memory. We poke `__pydantic_extra__` because
            # ChannelsConfig uses `extra="allow"` to store unknown
            # channels there.
            ws_dict = {
                "enabled": True,
                "websocket_requires_token": False,
            }
            try:
                extra = getattr(config.channels, "__pydantic_extra__", None)
                if isinstance(extra, dict):
                    extra["websocket"] = ws_dict
                else:
                    setattr(config.channels, "websocket", ws_dict)
            except Exception:  # noqa: BLE001
                pass
        else:
            if hasattr(ws_section, "enabled"):
                if not getattr(ws_section, "enabled", False):
                    try:
                        ws_section.enabled = True
                    except Exception:  # noqa: BLE001
                        pass
            elif isinstance(ws_section, dict):
                ws_section.setdefault("enabled", True)
    from durin.agent.tools.cron import CronTool
    from durin.agent.tools.message import MessageTool
    from durin.bus.queue import MessageBus
    from durin.channels.manager import ChannelManager
    from durin.channels.websocket import publish_runtime_model_update
    from durin.cron.service import CronService
    from durin.cron.types import CronJob
    from durin.providers.factory import (
        build_provider_snapshot,
        load_default_preset,
        load_provider_snapshot,
    )
    from durin.session.manager import SessionManager

    port = port if port is not None else config.gateway.port

    from durin.cli.gateway_daemon import AlreadyRunningError, acquire_gateway_singleton

    try:
        acquire_gateway_singleton()
    except AlreadyRunningError:
        console.print("[red]Error: another gateway instance is already running.[/red]")
        raise typer.Exit(1) from None

    console.print(f"{__logo__} Starting durin gateway version {__version__} on port {port}...")
    sync_workspace_templates(config.workspace_path)
    seed_example_personas()  # one-time: example personas into config (marker-guarded)
    register_config_modes(config.agent_modes)  # custom modes → agent-mode registry
    bus = MessageBus()
    try:
        provider_snapshot = build_provider_snapshot(config)
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    session_manager = SessionManager(config.workspace_path)

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path, run_history_max=config.cron.run_history_max)

    # Create agent with cron service
    agent = AgentLoop.from_config(
        config, bus,
        provider=provider_snapshot.provider,
        model=provider_snapshot.model,
        context_window_tokens=provider_snapshot.context_window_tokens,
        cron_service=cron,
        session_manager=session_manager,
        provider_snapshot_loader=load_provider_snapshot,
        default_preset_loader=load_default_preset,
        runtime_model_publisher=lambda model, preset: publish_runtime_model_update(
            bus,
            model,
            preset,
        ),
        provider_signature=provider_snapshot.signature,
    )

    from durin.agent.loop import UNIFIED_SESSION_KEY
    from durin.bus.events import OutboundMessage

    def _channel_session_key(channel: str, chat_id: str) -> str:
        return (
            UNIFIED_SESSION_KEY
            if config.agents.defaults.unified_session
            else f"{channel}:{chat_id}"
        )

    async def _deliver_to_channel(
        msg: OutboundMessage, *, record: bool = False, session_key: str | None = None,
    ) -> None:
        """Publish a user-visible message and mirror it into that channel's session."""
        metadata = dict(msg.metadata or {})
        record = record or bool(metadata.pop("_record_channel_delivery", False))
        if metadata != (msg.metadata or {}):
            msg = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                reply_to=msg.reply_to,
                media=msg.media,
                metadata=metadata,
                buttons=msg.buttons,
            )
        if (
            record
            and msg.channel != "cli"
            and msg.content.strip()
            and hasattr(session_manager, "get_or_create")
            and hasattr(session_manager, "save")
        ):
            key = session_key or _channel_session_key(msg.channel, msg.chat_id)
            session = session_manager.get_or_create(key)
            extra: dict[str, Any] = {"_channel_delivery": True}
            if msg.media:
                extra["media"] = list(msg.media)
            session.add_message("assistant", msg.content, **extra)
            session_manager.save(session)
        await bus.publish_outbound(msg)

    message_tool = getattr(agent, "tools", {}).get("message")
    if isinstance(message_tool, MessageTool):
        message_tool.set_send_callback(_deliver_to_channel)

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        # The memory dream runs the extract/refine/skill passes directly
        # (not through the agent loop). Sync passes offloaded to a thread so
        # the cron loop stays responsive during the LLM calls.
        if job.name == "memory_dream":
            import asyncio as _asyncio

            workspace = config.workspace_path
            from durin.memory.always_on_dream import run_always_on_pass
            from durin.memory.distill_dream import (
                run_curate_topics_pass,
                run_distill_reference_pass,
                run_seed_entities_pass,
            )
            from durin.memory.dream_passes import (
                dream_vector_index,
                run_derived_from_pass,
                run_extract_pass,
                run_refine_pass,
                run_skill_extract_pass,
            )
            from durin.memory.model_resolve import resolve_aux_preset
            from durin.workflow.workflow_improve_dream import run_workflow_improve_pass

            # The daily cron runs the extract pass (sessions → entity attributes),
            # the skill-extract pass (sessions → reusable procedures as skills),
            # then the refine pass (dedup). Writes go through memory_writer /
            # skill_write.
            # One resolution for the whole dream run: the memory preset pairs the
            # model WITH its provider; passing just the name keeps the passes'
            # default_llm_invoke (which resolves the same preset) consistent.
            model = resolve_aux_preset(config, purpose="memory").model
            _cron_max_s = config.memory.dream.max_seconds_per_run
            _absorb = config.memory.dream.auto_absorb
            _discover = config.memory.dream.discover_enabled
            _skill_signals = config.memory.dream.skill_signals_enabled
            _distill_refs = config.memory.dream.distill_references_enabled
            _seed_entities = config.memory.dream.seed_entities_from_docs_enabled
            _curate_topics = config.memory.dream.curate_topics_enabled
            _learnings = config.memory.dream.learnings_sweep_enabled
            _dream_error: Exception | None = None
            from datetime import datetime, timezone
            _run_started = datetime.now(timezone.utc)
            _vi = dream_vector_index(workspace, config)

            # Live feedback: persist this run's telemetry so the dream digest
            # reflects it afterward, AND tee each activity event to the webui
            # over the websocket as it happens. The passes run via
            # asyncio.to_thread, which copies the current context, so binding the
            # logger here propagates it into those threads; emit_tool_event then
            # writes JSONL and fans out to the DreamProgressSink.
            from durin.channels.websocket import publish_dream_progress
            from durin.memory.dream_live import DreamProgressSink
            from durin.telemetry.logger import (
                bind_telemetry,
                get_session_logger,
                reset_telemetry,
            )

            _dream_loop = _asyncio.get_running_loop()

            def _publish_dream(payload: dict) -> None:
                # Pass threads can't touch the asyncio bus queue directly; hop
                # back onto the loop thread first.
                _dream_loop.call_soon_threadsafe(
                    publish_dream_progress, bus, payload)

            _dream_tlog = get_session_logger("cron_dream")
            _dream_tlog.add_sink(DreamProgressSink(_publish_dream))
            _dream_ttok = bind_telemetry(_dream_tlog)
            ex, df, sk, rf, ao, wi = {}, {}, {}, {}, {}, {}
            publish_dream_progress(bus, {"kind": "run_started"})
            try:
                ex = await _asyncio.to_thread(
                    run_extract_pass, workspace, model=model,
                    max_seconds=_cron_max_s, discover=_discover,
                    skill_signals=_skill_signals,
                    learnings=_learnings,
                    confidence_threshold=_absorb.confidence_threshold,
                    semantic_distance_threshold=_absorb.semantic_distance_threshold,
                    vector_index=_vi)
                df = await _asyncio.to_thread(
                    run_derived_from_pass, workspace, model=model, max_seconds=_cron_max_s)
                # Distil ingested reference documents into outline sidecars —
                # the "know the book" index. Independent of entity merges, so it
                # slots right after the source-link pass.
                di = (
                    await _asyncio.to_thread(
                        run_distill_reference_pass, workspace, model=model,
                        max_seconds=_cron_max_s)
                    if _distill_refs
                    else {"references": 0, "outlined": 0, "skipped": 0, "duration_ms": 0}
                )
                # Seed candidate entities from each distilled document's outline
                # (derived_from = the document). Reads outlines the distil step
                # just wrote, so it follows it. The refine pass dedups later.
                se = (
                    await _asyncio.to_thread(
                        run_seed_entities_pass, workspace, model=model,
                        max_seconds=_cron_max_s)
                    if _seed_entities
                    else {"references": 0, "seeded_docs": 0, "entities": 0,
                          "skipped": 0, "duration_ms": 0}
                )
                # Curate the library's topic index from the distilled abstracts —
                # the clean, stable "Covers:" map the always-on awareness reads.
                # Reads the outlines the distil step wrote, so it follows it.
                ct = (
                    await _asyncio.to_thread(
                        run_curate_topics_pass, workspace, model=model,
                        max_seconds=_cron_max_s)
                    if _curate_topics
                    else {"topics": 0, "skipped": True, "duration_ms": 0}
                )
                logger.info(
                    "memory_dream cron: distill(references={} outlined={} "
                    "skipped={} {}ms) seed_entities(docs={} entities={} {}ms) "
                    "topics(n={} {}ms)",
                    di.get("references", 0), di.get("outlined", 0),
                    di.get("skipped", 0), di.get("duration_ms", 0),
                    se.get("seeded_docs", 0), se.get("entities", 0),
                    se.get("duration_ms", 0),
                    ct.get("topics", 0), ct.get("duration_ms", 0))
                sk = await _asyncio.to_thread(run_skill_extract_pass, workspace, model=model)
                rf = await _asyncio.to_thread(
                    run_refine_pass, workspace, model=model,
                    enabled=_absorb.enabled,
                    confidence_threshold=_absorb.confidence_threshold,
                    escalate_floor=_absorb.escalate_floor,
                    semantic_distance_threshold=_absorb.semantic_distance_threshold,
                    run_started_at=_run_started,
                    vector_index=_vi)
                # Relation-vocabulary hygiene: canonicalise entity-relation type
                # labels so graph edges line up, and report the vocabulary for
                # supervision. Runs after refine (which merges entities and their
                # relations).
                from durin.memory.relation_hygiene import run_consolidate_relations_pass
                rh = (
                    await _asyncio.to_thread(
                        run_consolidate_relations_pass, workspace,
                        max_seconds=_cron_max_s)
                    if config.memory.dream.consolidate_relations_enabled
                    else {"types_before": 0, "types_after": 0,
                          "pages_changed": 0, "merged_duplicates": 0, "duration_ms": 0}
                )
                logger.info(
                    "memory_dream cron: relations(types {}→{} pages={} merged={} {}ms)",
                    rh.get("types_before", 0), rh.get("types_after", 0),
                    rh.get("pages_changed", 0), rh.get("merged_duplicates", 0),
                    rh.get("duration_ms", 0))
                ao = await _asyncio.to_thread(
                    run_always_on_pass, workspace, model=model,
                    token_budget=config.memory.dream.always_on_token_budget)
                # Workflow self-improvement: inert unless a workflow opts into
                # improvement_mode 'manual'/'auto' (off by default).
                wi = await _asyncio.to_thread(run_workflow_improve_pass, workspace, model=model)
                logger.info(
                    "memory_dream cron: workflow_improve(workflows={} proposals={})",
                    wi.get("workflows", 0), wi.get("proposals", 0))
                logger.info(
                    "memory_dream cron: extract(sessions={} entities={} {}ms yielded={}) "
                    "derived_from(links={} sessions={} {}ms) "
                    "skills(touched={} {}ms) refine(merged={} kept={} {}ms) "
                    "always_on(pinned={} {}tok {}ms)",
                    ex["sessions"], ex["entities"], ex.get("duration_ms", 0),
                    ex.get("yielded", False),
                    df.get("links", 0), df.get("sessions", 0), df.get("duration_ms", 0),
                    sk.get("skills_touched", 0),
                    sk.get("duration_ms", 0), len(rf.get("merged", [])),
                    len(rf.get("kept_separate", [])), rf.get("duration_ms", 0),
                    ao.get("selected", 0), ao.get("tokens", 0), ao.get("duration_ms", 0),
                )
            except Exception as _dream_exc:
                logger.exception("memory_dream cron failed")
                _dream_error = _dream_exc

            # Skills improved by the curation pass (edits applied to existing
            # skills) — distinct from skills CREATED by the skill-extract pass.
            # Initialized here so the run summary has it even if curation fails.
            _skills_improved = 0
            try:
                from durin.agent.skill_curation import curate_catalog
                from durin.memory.llm_invoke import default_llm_invoke

                def _judge(prompt: str) -> str:
                    # ONE completion via the memory preset (model + provider),
                    # using the same call shape the refine pass's absorb judge uses.
                    return default_llm_invoke(prompt).text

                from durin.agent.skill_drift import check_upstream_drift
                from durin.agent.skill_usage import collect_recent_skill_calls
                _allowlist = list(config.skills.security.allowlist)
                _usage = collect_recent_skill_calls(workspace, within_hours=24)
                # Off the event loop: curation's sync judge (and the agentic
                # restructure executor's asyncio.run) must run in a worker thread,
                # not on the gateway's loop, or they block HTTP serving / raise
                # "asyncio.run from a running loop".
                summary = await _asyncio.to_thread(
                    curate_catalog, workspace, judge=_judge, usage=_usage,
                    drift_check=check_upstream_drift, allowlist=_allowlist)
                _skills_improved = summary.get("applied", 0)
                _obs = summary.get("observations", {})
                logger.info(
                    "skill curation: reviewed={} applied={} deferred={} backfilled={} "
                    "judge_parse_failed={} "
                    "obs_applied={} obs_declined={} obs_kept={} obs_open={} principles={}",
                    summary["reviewed"], summary["applied"], summary["deferred"],
                    summary.get("backfilled", 0), summary.get("judge_parse_failed", False),
                    _obs.get("applied", 0), _obs.get("declined", 0), _obs.get("kept", 0),
                    _obs.get("open", 0), summary.get("principles", 0),
                )
            except Exception:
                logger.exception("skill curation step (non-fatal) failed")

            # Skill suggestions for MANUAL skills: propose curation actions into
            # the dream bandeja for user review (never auto-applied). Gated +
            # best-effort: a failure here must not abort the dream cron.
            if config.memory.dream.skill_suggestions_enabled:
                try:
                    from durin.agent.skill_curation import suggest_manual_skills
                    from durin.memory.llm_invoke import default_llm_invoke

                    def _sg_judge(prompt: str) -> str:
                        return default_llm_invoke(prompt).text

                    from durin.agent.skill_usage import collect_recent_skill_calls
                    _sg_usage = collect_recent_skill_calls(workspace, within_hours=24)
                    _sg = await _asyncio.to_thread(
                        suggest_manual_skills, workspace, judge=_sg_judge, usage=_sg_usage)
                    logger.info(
                        "skill suggestions: reviewed={} suggested={} suppressed={}",
                        _sg["reviewed"], _sg["suggested"], _sg["suppressed"])
                except Exception:
                    logger.exception("skill suggestions step (non-fatal) failed")

            # Reap stale per-run cron sessions (cron:{id}:run:{ms}). These are
            # created on every agent_turn cron execution and never otherwise
            # removed; run_session_retention_hours bounds how long they live.
            try:
                from durin.cron.reaper import reap_expired_run_sessions

                reap_expired_run_sessions(
                    session_manager, config.cron.run_session_retention_hours
                )
            except Exception:
                logger.exception("cron run-session reaper (non-fatal) failed")

            # One summary entry per run — even an empty run leaves a visible
            # "ran, nothing new" line in the Dream feed instead of silently
            # updating only the last-run time. Persisted via the still-bound
            # logger and teed live by the DreamProgressSink.
            from durin.agent.tools._telemetry import emit_tool_event
            _run_summary = {
                "sessions": ex.get("sessions", 0) if isinstance(ex, dict) else 0,
                "entities": ex.get("entities", 0) if isinstance(ex, dict) else 0,
                "merged": len(rf.get("merged", [])) if isinstance(rf, dict) else 0,
                "skills_created": sk.get("skills_touched", 0) if isinstance(sk, dict) else 0,
                "skills_improved": _skills_improved,
            }
            emit_tool_event("memory.dream.run_summary", _run_summary)
            # Durable record so the "last run" card + history survive the telemetry
            # window / retention (telemetry is the live feed; this is the truth).
            from durin.memory.dream_runs import record_dream_run
            record_dream_run(workspace, _run_summary)

            # Unbind telemetry and tell the webui the run is over (success or
            # not) so its "running" pulse always stops. Runs before the error
            # re-raise below; the passes/curation/reaper above each catch their
            # own exceptions, so nothing escapes between here and bind.
            reset_telemetry(_dream_ttok)
            publish_dream_progress(bus, {
                "kind": "run_finished",
                "ok": _dream_error is None,
            })

            if _dream_error is not None:
                # Surface the failure so _execute_job records status="error"
                # (not a false "ok") — the consolidation passes did not complete.
                raise RuntimeError(
                    f"memory_dream consolidation failed: {_dream_error}"
                ) from _dream_error
            return None

        # Loop triggers fire the loops runtime directly (not the agent turn below).
        if job.payload.kind == "loop_trigger":
            if not job.payload.loop:
                logger.warning("loop_trigger cron job {} has no loop name; skipping", job.id)
                return None
            await loops_runtime.try_fire(job.payload.loop, source="cron")
            return None

        from durin.cron.prompting import build_cron_turn_prompt
        from durin.utils.evaluator import evaluate_response

        prompt = build_cron_turn_prompt(job.payload.mode, job.payload.message)
        session_key = job.payload.session_key or f"cron:{job.id}"

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)

        message_record_token = None
        if isinstance(message_tool, MessageTool):
            message_record_token = message_tool.set_record_channel_delivery(True)

        try:
            resp = await agent.process_direct(
                prompt,
                session_key=session_key,
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
                on_progress=None,
                model_preset=job.payload.model,
                persona=job.payload.persona,
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)
            if isinstance(message_tool, MessageTool) and message_record_token is not None:
                message_tool.reset_record_channel_delivery(message_record_token)

        response = resp.content if resp else ""

        if job.payload.deliver and isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            should_notify = await evaluate_response(
                response, prompt, agent.provider, agent.model,
            )
            if should_notify:
                await _deliver_to_channel(
                    OutboundMessage(
                        channel=job.payload.channel or "cli",
                        chat_id=job.payload.to,
                        content=response,
                        metadata=dict(job.payload.channel_meta),
                    ),
                    record=True,
                    session_key=job.payload.session_key,
                )
        return response

    cron.on_job = on_cron_job

    # Loops: cron-triggered runs, verified against an LLM judge, with operator
    # asks/escalations delivered through the same channel path cron uses. A
    # dedicated WorkflowsService instance (workflows_service isn't built yet at
    # this point — build_service_registry constructs its own further down).
    import time

    from durin.loops import channel_meta as _loop_channel_meta
    from durin.loops import queue as _loops_queue
    from durin.loops.judge import build_filter_prompt as _loop_build_filter_prompt
    from durin.loops.judge import build_prompt as _loop_build_prompt
    from durin.loops.judge import parse_filter_verdict as _loop_parse_filter_verdict
    from durin.loops.judge import parse_verdict as _loop_parse_verdict
    from durin.loops.matcher import TriggerMatcher
    from durin.loops.runtime import LoopsRuntime
    from durin.loops.store import load_loop as _load_loop
    from durin.memory.model_resolve import resolve_aux_preset
    from durin.service.workflows import WorkflowsService as _LoopsWorkflowsService

    _loops_workflows_service = _LoopsWorkflowsService(
        config.workspace_path, app_config=config, sessions=session_manager,
    )

    def _loop_model_preset_ref() -> str | None:
        """Resolve agents.aux_models.loops to the "provider model" picker-ref
        string process_direct's model_preset expects. None means "no aux
        override" — loops is the one aux purpose that does NOT fall back to a
        separately resolved default preset (see durin.memory.model_resolve),
        so the call rides the agent's own live model instead."""
        preset = resolve_aux_preset(config, purpose="loops")
        return f"{preset.provider} {preset.model}" if preset is not None else None

    async def _loop_judge(intent: str, assertions: list[str], evidence: str) -> dict:
        prompt = _loop_build_prompt(intent, assertions, evidence)
        resp = await agent.process_direct(
            # loop:judge:run:{ms} — matches durin.cron.reaper's run-session
            # pattern so judge sessions are reaped like cron run sessions
            # instead of accumulating forever.
            prompt, session_key=f"loop:judge:run:{int(time.time() * 1000)}",
            model_preset=_loop_model_preset_ref(),
        )
        return _loop_parse_verdict(resp.content if resp else "")

    async def _loop_semantic_judge(condition: str, summary: str) -> bool:
        prompt = _loop_build_filter_prompt(condition, summary)
        resp = await agent.process_direct(
            # loop:filter:run:{ms} — reaped by the same run-session pattern as
            # loop:judge:run: (see durin.cron.reaper).
            prompt, session_key=f"loop:filter:run:{int(time.time() * 1000)}",
            model_preset=_loop_model_preset_ref(),
        )
        return _loop_parse_filter_verdict(resp.content if resp else "")

    async def _on_loop_ask(loop: str, run_id: str, kind: str, text: str) -> None:
        try:
            spec = _load_loop(config.workspace_path, loop)
        except Exception:
            return
        if not spec.operator_channel:
            return
        try:
            await _deliver_to_channel(
                OutboundMessage(channel=spec.operator_channel, chat_id=spec.operator_to or "direct", content=text),
            )
        except Exception:
            logger.exception("loop operator notification (non-fatal) failed")

    async def _on_counterpart_ask(loop: str, run_id: str, origin: dict, text: str) -> None:
        """Deliver a waiting_info question back to the channel that triggered
        the run (e.g. the same email thread), so a reply into that thread
        wakes the run via the matcher's claim lookup."""
        if origin.get("channel") == "webhook":
            # By design, not an error: a webhook origin has no reply channel
            # to deliver into (build_reply has no webhook case and would
            # raise). The question stays visible in the run's Activity feed;
            # a counterpart resumes the run via a correlate-matched wake POST
            # instead of a channel reply.
            logger.debug("loop counterpart ask: webhook origin has no reply channel; ask remains in Activity")
            return
        try:
            await bus.publish_outbound(_loop_channel_meta.build_reply(origin, text))
        except Exception:
            logger.exception("loop counterpart delivery (non-fatal) failed")

    loops_runtime = LoopsRuntime(
        config.workspace_path,
        workflow_exec=_loops_workflows_service.execute,
        judge=_loop_judge,
        keep_runs=config.loops.keep_runs,
        check_timeout_s=config.loops.check_timeout_s,
        on_operator_ask=_on_loop_ask,
        on_counterpart_ask=_on_counterpart_ask,
        queue_ttl_s=config.loops.queue_ttl_s,
    )
    agent.register_loops_tool(loops_runtime)

    # Route inbound channel messages through the trigger matcher BEFORE they
    # reach the normal agent turn: a claim wake or a fired/queued loop trigger
    # consumes the message, everything else falls through unchanged.
    _loops_matcher = TriggerMatcher(
        config.workspace_path,
        runtime=loops_runtime,
        semantic_judge=_loop_semantic_judge,
        queue_ttl_s=config.loops.queue_ttl_s,
        enqueue=lambda loop, ev: _loops_queue.push(config.workspace_path, loop, ev),
    )
    bus.add_inbound_interceptor(_loops_matcher.handle_inbound)

    # Webhook trigger ingress (POST /api/v1/hooks/{hook}, wired into the
    # unified gateway app below): shares the matcher's wake/fire/queue
    # decision instead of duplicating it — see durin/loops/hooks.py.
    from durin.loops.hooks import HookDispatcher

    loops_hook_dispatcher = HookDispatcher(_loops_matcher)

    def _webui_runtime_model_name() -> str | None:
        model = getattr(agent, "model", None)
        if isinstance(model, str):
            stripped = model.strip()
            return stripped or None
        return None

    def _webui_runtime_model_preset() -> str | None:
        preset = getattr(agent, "model_preset", None)
        if isinstance(preset, str):
            stripped = preset.strip()
            return stripped or None
        return None

    # Create channel manager (forwards SessionManager so the WebSocket channel
    # can serve the embedded webui's REST surface).
    channels = ChannelManager(
        config,
        bus,
        session_manager=session_manager,
        webui_runtime_model_name=_webui_runtime_model_name,
        webui_runtime_model_preset=_webui_runtime_model_preset,
        webui_runtime_concurrency_snapshot=agent.build_concurrency_snapshot,
        cron_service=cron,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    async def _health_server(host: str, health_port: int):
        """Lightweight HTTP health endpoint on the gateway port."""
        import json as _json

        async def handle(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=5)
            except (asyncio.TimeoutError, ConnectionError):
                writer.close()
                return

            request_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
            method, path = "", ""
            parts = request_line.split(" ")
            if len(parts) >= 2:
                method, path = parts[0], parts[1]

            if method == "GET" and path == "/health":
                body = _json.dumps({"status": "ok"})
                resp = (
                    f"HTTP/1.0 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )
            else:
                body = "Not Found"
                resp = (
                    f"HTTP/1.0 404 Not Found\r\n"
                    f"Content-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )

            writer.write(resp.encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, host, health_port)
        console.print(f"[green]✓[/green] Health endpoint: http://{host}:{health_port}/health")
        async with server:
            await server.serve_forever()
    from durin.cron.types import CronJob, CronPayload, CronSchedule

    # Register the memory dream system job: the daily extract/refine/skill
    # passes that consolidate sessions into memory/entities/<type>/<slug>.md
    # pages + skills.
    mem_dream_cfg = config.memory.dream
    if mem_dream_cfg.enabled:
        cron.register_system_job(CronJob(
            id="memory_dream",
            name="memory_dream",
            schedule=CronSchedule(
                kind="cron",
                expr=mem_dream_cfg.cron,
                tz=config.agents.defaults.timezone,
            ),
            payload=CronPayload(kind="system_event"),
        ))
        console.print(
            f"[green]✓[/green] Memory dream (entity-centric): "
            f"cron {mem_dream_cfg.cron}"
        )

    # Prune system crons left in the store by a previous build but no longer
    # managed here (e.g. the legacy 2h `dream` retired in the entity-centric
    # redesign) so they don't linger in the UI / fire with no handler. Keep this
    # set in sync with the system jobs registered above.
    pruned = cron.prune_orphaned_system_jobs({"memory_dream"})
    if pruned:
        console.print(
            f"[green]✓[/green] Cron: pruned orphaned system job(s): {', '.join(pruned)}"
        )

    # Reconcile loop trigger cron jobs with the stored loop definitions.
    # Best-effort: parse-time validation (LoopSpec._parse_trigger) makes a bad
    # schedule unrepresentable going forward, but a legacy/hand-edited loop
    # file could still fail here — one bad loop must not crash gateway boot.
    from durin.loops.cron_sync import sync_all as _loops_sync_all

    try:
        _loops_sync_all(cron, config.workspace_path)
    except Exception:
        logger.exception("loops cron sync (non-fatal) failed")

    # Wire the post-compaction + session-close hooks so dream picks up
    # consolidated context while the signal is fresh. Background daemon
    # threads keep the agent loop responsive. The reactive dream gate's
    # lock + throttle absorbs collisions with the daily cron. `hasattr`
    # checks keep test scaffolds (_FakeAgentLoop without consolidator /
    # on_session_close) working — production AgentLoop always has both
    # attributes.
    if mem_dream_cfg.enabled and (
        mem_dream_cfg.post_compaction or mem_dream_cfg.on_session_close
    ):
        import threading as _threading_dream

        from durin.memory.dream_passes import ReactiveDreamGate
        # Shared by both reactive triggers: a lock so two events don't run
        # overlapping passes + a throttle so a burst of session closes is
        # absorbed (replaces the legacy cross-process .dream.lock + cooldown).
        _dream_gate = ReactiveDreamGate()
        _dream_min_s = mem_dream_cfg.min_seconds_between_runs
        _dream_max_s = mem_dream_cfg.max_seconds_per_run

        def _spawn_dream(trigger: str, session_key: str) -> None:
            ws = config.workspace_path

            def _run() -> None:
                import time as _time_dream

                from durin.agent.tools._telemetry import emit_tool_event
                from durin.telemetry.logger import (
                    bind_telemetry,
                    get_session_logger,
                    reset_telemetry,
                )
                # Skip when a pass is already running or one ran too recently —
                # the per-session cursor makes a skipped run harmless.
                skip = _dream_gate.try_begin(_dream_min_s)
                if skip:
                    with suppress(Exception):
                        emit_tool_event(
                            "memory.dream.throttled",
                            {"trigger": trigger, "reason": skip},
                        )
                    logger.debug("reactive dream skipped ({}): {}", trigger, skip)
                    return
                t_run = _time_dream.perf_counter()
                # Fresh daemon thread = no inherited context; bind a telemetry
                # logger so the reactive extract's emits persist and the dream
                # digest sees this run (without it emit_tool_event is a no-op).
                _rtok = bind_telemetry(get_session_logger("reactive_dream"))
                try:
                    # Reactive EXTRACT — when a session closes or compacts,
                    # extract its new turns into entity attributes immediately
                    # (the frequent dream, event-driven; the per-session cursor
                    # makes it idempotent). Refine stays on the daily cron.
                    from durin.memory.dream_passes import dream_vector_index, run_extract_pass
                    from durin.memory.model_resolve import resolve_aux_preset
                    # Pass the vector index so source-side semantic dedup runs on
                    # the reactive path too (where most turns are processed first
                    # — the cron rarely re-sees them). Bounded cost: the reactive
                    # dream is throttled (min_seconds_between_runs) and the
                    # embedding model loads lazily.
                    out = run_extract_pass(
                        ws, model=resolve_aux_preset(config, purpose="memory").model,
                        max_seconds=_dream_max_s,
                        discover=config.memory.dream.discover_enabled,
                        skill_signals=config.memory.dream.skill_signals_enabled,
                        learnings=config.memory.dream.learnings_sweep_enabled,
                        confidence_threshold=config.memory.dream.auto_absorb.confidence_threshold,
                        semantic_distance_threshold=config.memory.dream.auto_absorb.semantic_distance_threshold,
                        vector_index=dream_vector_index(ws, config),
                    )
                    logger.info(
                        "reactive dream done ({}): {} session(s), {} attribute "
                        "update(s), yielded={}, {}ms",
                        trigger, out["sessions"], out["entities"], out["yielded"],
                        int((_time_dream.perf_counter() - t_run) * 1000),
                    )
                    # Record a run summary so reactive runs also surface in the
                    # Dream feed / "última corrida" card. The reactive path is
                    # extract-only (refine/skills are cron-only), so those are 0.
                    _reactive_summary = {
                        "sessions": out.get("sessions", 0),
                        "entities": out.get("entities", 0),
                        "merged": 0,
                        "skills_created": 0,
                        "skills_improved": 0,
                    }
                    emit_tool_event("memory.dream.run_summary", _reactive_summary)
                    from durin.memory.dream_runs import record_dream_run
                    record_dream_run(workspace, _reactive_summary)
                except Exception:
                    logger.exception("{} dream failed ({})", trigger, session_key)
                finally:
                    reset_telemetry(_rtok)
                    _dream_gate.end()

            _threading_dream.Thread(
                target=_run, daemon=True, name=f"dream-{trigger}",
            ).start()

        if mem_dream_cfg.post_compaction and hasattr(agent, "consolidator"):
            agent.consolidator.on_post_compaction = (
                lambda k: _spawn_dream("post_compaction", k)
            )
            console.print(
                "[green]✓[/green] Memory dream: post-compaction trigger armed"
            )
        if mem_dream_cfg.on_session_close and hasattr(agent, "on_session_close"):
            agent.on_session_close = (
                lambda k: _spawn_dream("session_close", k)
            )
            console.print(
                "[green]✓[/green] Memory dream: session-close trigger armed"
            )

    async def _open_browser_when_ready() -> None:
        """Wait for the gateway to bind, then point the user's browser at the webui."""
        if not open_browser_url:
            return
        import webbrowser
        # Channels start asynchronously; a short poll lets us avoid racing the bind.
        for _ in range(40):  # ~4s max
            try:
                reader, writer = await asyncio.open_connection(
                    config.gateway.host or "127.0.0.1", port
                )
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        try:
            webbrowser.open(open_browser_url)
            console.print(f"[green]✓[/green] Opened browser at {open_browser_url}")
        except Exception as e:
            console.print(f"[yellow]Could not open browser ({e}); visit {open_browser_url}[/yellow]")

    def _check_ports_free() -> bool:
        """Configured ports are authoritative — we never silently move them
        (that breaks a user's URL/scripts and is unpredictable in dev/test). If
        one is genuinely taken (another instance listening), report it with clear
        guidance. A socket merely in TIME_WAIT from a just-stopped daemon reads
        as available, so a restart re-binds the same port."""
        from durin.config.home import durin_home
        from durin.utils.net import port_is_available

        ws = getattr(config.channels, "websocket", None)
        if isinstance(ws, dict):
            ws_host, ws_port = ws.get("host", "127.0.0.1"), ws.get("port", 8765)
        elif ws is not None:
            ws_host = getattr(ws, "host", "127.0.0.1") or "127.0.0.1"
            ws_port = getattr(ws, "port", 8765) or 8765
        else:
            ws_host, ws_port = "127.0.0.1", 8765
        for label, host_, port_ in (
            ("gateway", config.gateway.host, port),
            ("websocket dashboard", ws_host, ws_port),
        ):
            if not port_is_available(host_, port_):
                console.print(
                    f"[red]Port {port_} ({label}) on {host_} is already in use — "
                    f"another durin instance?[/red]"
                )
                console.print(
                    f"  Set this instance's port in its config "
                    f"(DURIN_HOME={durin_home()}), then retry."
                )
                return False
        return True

    async def run():
        # Without an explicit SIGTERM handler, Python's default action
        # terminates the process instantly — the `finally` block below
        # never runs, so `durin gateway stop` (which sends SIGTERM) would
        # skip the session flush and lose unsaved work, and the daemon
        # would die with nothing in the log. Install async signal
        # handlers so SIGTERM/SIGINT/SIGHUP all trigger a logged,
        # graceful shutdown.
        if not _check_ports_free():
            return
        loop = asyncio.get_running_loop()
        gathered: asyncio.Future | None = None
        api_server = None      # SP4: optional 2nd-port uvicorn front door
        unified_server = None  # Step 4: unified uvicorn on the WS port (default path)

        def _request_shutdown(signame: str) -> None:
            logger.info("Gateway received {}; shutting down gracefully.", signame)
            if unified_server is not None:
                unified_server.should_exit = True
            if api_server is not None:
                api_server.should_exit = True
            if gathered is not None and not gathered.done():
                gathered.cancel()

        for _sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
            if _sig is None:
                continue
            try:
                loop.add_signal_handler(_sig, _request_shutdown, _sig.name)
            except (NotImplementedError, RuntimeError):
                pass  # add_signal_handler is unsupported on Windows

        try:
            await cron.start()

            # Unified uvicorn server: the gateway serves WS chat + /api/v1 + SPA
            # via a single Starlette app on the websocket channel's port.  The
            # channel acts purely as the handler.
            import contextlib as _contextlib_unified

            import uvicorn as _uvicorn_unified

            from durin.agent.mcp_runtime import McpRuntime as _McpRuntime
            from durin.api.asgi import build_gateway_http_app as _build_gw_app
            from durin.service.wiring import build_service_registry as _build_reg

            _ws_channel = channels.get_channel("websocket")
            if _ws_channel is not None:
                _unified_registry = _build_reg(
                    config=config,
                    session_manager=session_manager,
                    cron_service=cron,
                    bus=bus,
                    mcp_runtime=_McpRuntime(agent),
                    subagent_manager=agent.subagents,
                    channel_manager=channels,
                    loops_runtime=loops_runtime,
                    tool_registry_resolver=lambda: agent.tools,
                    on_config_changed=agent.reload_app_config,
                    on_default_changed=agent.apply_default_model_live,
                )
                # Static token lives on the websocket channel config.
                _ws_cfg_u = getattr(config.channels, "websocket", None)
                if isinstance(_ws_cfg_u, dict):
                    _static_token_u = _ws_cfg_u.get("token") or ""
                elif _ws_cfg_u is not None:
                    _static_token_u = getattr(_ws_cfg_u, "token", "") or ""
                else:
                    _static_token_u = ""

                _unified_app = _build_gw_app(
                    _ws_channel,
                    _unified_registry,
                    auth=_unified_registry.get("auth"),
                    static_token=_static_token_u,
                    static_dist_path=_ws_channel._static_dist_path,
                    hook_dispatcher=loops_hook_dispatcher,
                )
                _ws_port = _ws_channel.config.port  # type: ignore[attr-defined]
                _ws_host = _ws_channel.config.host  # type: ignore[attr-defined]
                _ws_ssl_cert = getattr(_ws_channel.config, "ssl_certfile", "") or ""
                _ws_ssl_key = getattr(_ws_channel.config, "ssl_keyfile", "") or ""
                _uvicorn_kwargs: dict = dict(
                    host=_ws_host,
                    port=_ws_port,
                    log_level="warning",
                    ws_max_size=_ws_channel.config.max_message_bytes,  # type: ignore[attr-defined]
                    ws_ping_interval=_ws_channel.config.ping_interval_s,  # type: ignore[attr-defined]
                    ws_ping_timeout=_ws_channel.config.ping_timeout_s,  # type: ignore[attr-defined]
                )
                if _ws_ssl_cert and _ws_ssl_key:
                    _uvicorn_kwargs["ssl_certfile"] = _ws_ssl_cert
                    _uvicorn_kwargs["ssl_keyfile"] = _ws_ssl_key
                unified_server = _uvicorn_unified.Server(
                    _uvicorn_unified.Config(_unified_app, **_uvicorn_kwargs)
                )
                # The gateway owns SIGINT/SIGTERM; uvicorn must not install its own.
                unified_server.capture_signals = _contextlib_unified.nullcontext
                _scheme = "wss" if (_ws_ssl_cert and _ws_ssl_key) else "ws"
                console.print(
                    f"[green]✓[/green] Unified gateway: "
                    f"{_scheme}://{_ws_host}:{_ws_port} "
                    f"(WS + /api/v1 + SPA)"
                )

            tasks = [
                agent.run(),
                channels.start_all(),
                _health_server(config.gateway.host, port),
            ]
            if unified_server is not None:
                tasks.append(unified_server.serve())

            if open_browser_url:
                tasks.append(_open_browser_when_ready())
            gathered = asyncio.gather(*tasks)
            await gathered
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("\nShutting down...")
        except Exception:
            import traceback

            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
            logger.error("Gateway crashed unexpectedly:\n{}", traceback.format_exc())
        finally:
            await agent.close_mcp()
            cron.stop()
            agent.stop()
            await channels.stop_all()
            # Flush all cached sessions to durable storage before exit.
            # This prevents data loss on filesystems with write-back
            # caching (rclone VFS, NFS, FUSE mounts, etc.).
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Shutdown: flushed {} session(s) to disk", flushed)

    asyncio.run(run())


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(
        None, "--message", "-m",
        help="One-shot message to send to the agent and exit (skips the TUI).",
    ),
    session_id: str | None = typer.Option(
        None, "--session", "-s",
        help=(
            "Session ID to open (e.g. `cli:work`). Without this, the TUI "
            "opens the most recently used session, or starts fresh if none exist."
        ),
    ),
    new_session: bool = typer.Option(
        False, "--new", "-n",
        help="Start a brand-new session with a timestamp id (`cli:YYYYMMDD_HHMMSS`).",
    ),
    resume: bool = typer.Option(
        False, "--resume", "-r",
        help="Open the session picker on launch instead of auto-selecting.",
    ),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show durin runtime logs during chat"),
    tui: bool = typer.Option(
        True, "--tui/--legacy",
        help=(
            "TUI is the default; pass --legacy to fall back to the prompt_toolkit "
            "REPL (single-line input, no streaming UI)."
        ),
    ),
):
    """Interact with the agent.

    Without flags, `durin agent` opens the rich TUI on your most recently
    used session. Use --new to start fresh, --resume to pick a session
    interactively, --session <id> for a specific one, or --legacy for
    the old single-line REPL.
    """
    from loguru import logger

    from durin.bus.queue import MessageBus
    from durin.cron.service import CronService

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)
    seed_example_personas()  # one-time: example personas into config (marker-guarded)
    register_config_modes(config.agent_modes)  # custom modes → agent-mode registry

    bus = MessageBus()

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path, run_history_max=config.cron.run_history_max)

    if logs:
        logger.enable("durin")
    else:
        logger.disable("durin")

    try:
        agent_loop = AgentLoop.from_config(
            config, bus,
            cron_service=cron,
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc

    # A `-m message` one-shot always falls through to the legacy
    # synchronous path below — TUI doesn't make sense for a single
    # message + exit.
    if message is not None:
        tui = False

    if tui:
        from durin.cli.sessions import fresh_session_id, most_recent_session
        from durin.cli.tui import run_durin_tui

        # Resolve which session the TUI should open.
        # Precedence: explicit --session > --new > --resume > most-recent > fresh default.
        auto_resume = False
        if session_id:
            if ":" in session_id:
                tui_channel, tui_chat_id = session_id.split(":", 1)
            else:
                tui_channel, tui_chat_id = "cli", session_id
        elif new_session:
            tui_channel, tui_chat_id = fresh_session_id()
        elif resume:
            # Let the TUI pop the session picker on mount.
            recent = most_recent_session(config.workspace_path)
            if recent:
                tui_channel, tui_chat_id = recent.channel, recent.chat_id
            else:
                tui_channel, tui_chat_id = "cli", "direct"
            auto_resume = True
        else:
            recent = most_recent_session(config.workspace_path)
            if recent:
                tui_channel, tui_chat_id = recent.channel, recent.chat_id
            else:
                tui_channel, tui_chat_id = "cli", "direct"

        run_durin_tui(
            agent_loop=agent_loop,
            cli_channel=tui_channel,
            cli_chat_id=tui_chat_id,
            markdown=markdown,
            auto_resume=auto_resume,
        )
        return

    # `--legacy` (or one-shot via -m) — fall through to the original REPL/one-shot.
    if session_id is None:
        session_id = "cli:direct"
    restart_notice = consume_restart_notice_from_env()
    if restart_notice and should_show_cli_restart_notice(restart_notice, session_id):
        _print_agent_response(
            format_restart_completed_message(restart_notice.started_at_raw),
            render_markdown=False,
        )

    # Shared reference for progress callbacks
    _thinking: ThinkingSpinner | None = None

    def _make_progress(renderer: StreamRenderer | None = None):
        async def _cli_progress(content: str, *, tool_hint: bool = False, reasoning: bool = False, reasoning_end: bool = False, agent_ui: dict[str, Any] | None = None, **_kwargs: Any) -> None:
            if agent_ui:
                from durin.cli.agent_ui_render import render_agent_ui
                target = renderer.console if renderer else console
                pause = renderer.pause_spinner() if renderer else (_thinking.pause() if _thinking else nullcontext())
                with pause:
                    if renderer:
                        renderer.ensure_header()
                    render_agent_ui(target, agent_ui)
                return
            ch = agent_loop.channels_config
            if reasoning_end:
                if ch and not ch.show_reasoning:
                    return
                await _flush_cli_reasoning(_thinking, renderer)
                return
            if reasoning:
                if ch and not ch.show_reasoning:
                    return
                await _print_cli_reasoning(content, _thinking, renderer)
                return
            if ch and tool_hint and not ch.send_tool_hints:
                return
            if ch and not tool_hint and not ch.send_progress:
                return
            await _print_cli_progress_line(content, _thinking, renderer)
        return _cli_progress

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            renderer = StreamRenderer(
                render_markdown=markdown,
                bot_name=config.agents.defaults.bot_name,
                bot_icon=config.agents.defaults.bot_icon,
            )
            response = await agent_loop.process_direct(
                message, session_id,
                on_progress=_make_progress(renderer),
                on_stream=renderer.on_delta,
                on_stream_end=renderer.on_end,
            )
            if not renderer.streamed:
                await renderer.close()
                print_kwargs: dict[str, Any] = {}
                if renderer.header_printed:
                    print_kwargs["show_header"] = False
                _print_agent_response(
                    response.content if response else "",
                    render_markdown=markdown,
                    metadata=response.metadata if response else None,
                    **print_kwargs,
                )
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from durin.bus.events import InboundMessage

        def _presets_for_completer() -> list[str]:
            names = set(agent_loop.model_presets or {})
            names.add("default")
            return sorted(names)

        def _footer_getter():
            from durin.cli.footer import build_footer_html, build_footer_text

            try:
                payload = build_footer_text(agent_loop, cli_channel, cli_chat_id)
                return build_footer_html(payload)
            except Exception:  # noqa: BLE001
                # Never let a footer-render error block input; fall back silent.
                return ""

        _init_prompt_session(
            workspace=Path(agent_loop.workspace),
            presets_getter=_presets_for_completer,
            footer_getter=_footer_getter,
        )
        _model, _preset_tag = _model_display(config)
        console.print(f"{__logo__} Interactive mode [bold blue]({_model})[/bold blue]{_preset_tag} — type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            nonlocal cli_chat_id
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[tuple[str, dict]] = []
            renderer: StreamRenderer | None = None

            async def _consume_outbound():
                nonlocal cli_chat_id
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

                        # /resume signals a session switch via metadata. The CLI's
                        # cli_chat_id determines the session_key of subsequent
                        # inbound publishes; updating it routes the next turn to
                        # the new session without restarting the process.
                        switch_to = (msg.metadata or {}).get("_switch_chat_id")
                        if switch_to and switch_to != cli_chat_id:
                            cli_chat_id = switch_to

                        if msg.metadata.get("_stream_delta"):
                            if renderer:
                                await renderer.on_delta(msg.content)
                            continue
                        if msg.metadata.get("_stream_end"):
                            if renderer:
                                await renderer.on_end(
                                    resuming=msg.metadata.get("_resuming", False),
                                )
                            continue
                        if msg.metadata.get("_streamed"):
                            turn_done.set()
                            continue

                        if await _maybe_print_interactive_progress(
                            msg,
                            renderer,
                            agent_loop.channels_config,
                            renderer,
                        ):
                            continue

                        if not turn_done.is_set():
                            if msg.content:
                                turn_response.append((msg.content, dict(msg.metadata or {})))
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(
                                msg.content,
                                render_markdown=markdown,
                                metadata=msg.metadata,
                            )

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        # Stop spinner before user input to avoid prompt_toolkit conflicts
                        if renderer:
                            renderer.stop_for_input()
                        user_input = _sanitize_surrogates(await _read_interactive_input_async())
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()
                        renderer = StreamRenderer(
                            render_markdown=markdown,
                            bot_name=config.agents.defaults.bot_name,
                            bot_icon=config.agents.defaults.bot_icon,
                        )

                        # Drag-and-drop: rewrite dragged image/audio paths to
                        # stable workspace copies and surface them via .media
                        # so the agent's existing multimodal pipeline sees them.
                        from durin.cli.dragdrop import (
                            process_dragged_paths,
                            transcribe_dragged_audio,
                        )

                        clean_input, media_paths = process_dragged_paths(
                            user_input, agent_loop.workspace
                        )

                        # Transcribe dragged audio before it reaches the agent:
                        # audio becomes text in clean_input and is dropped
                        # from media_paths.
                        if media_paths:
                            try:
                                from durin.service.transcription import (
                                    TranscriptionService,
                                )

                                stt_svc = TranscriptionService.from_config(
                                    config.transcription
                                )
                                clean_input, media_paths = await transcribe_dragged_audio(
                                    value=clean_input,
                                    media=media_paths,
                                    workspace=agent_loop.workspace,
                                    service=stt_svc,
                                    mode=config.transcription.mode,
                                )
                            except Exception:  # noqa: BLE001
                                pass

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=clean_input,
                            media=media_paths,
                            metadata={"_wants_stream": True},
                        ))

                        await turn_done.wait()

                        while True:
                            blocking = False
                            if turn_response:
                                content, meta = turn_response[0]
                                blocking = bool(meta.get("_block_input_until_response"))
                                if content and not meta.get("_streamed"):
                                    print_kwargs: dict[str, Any] = {}
                                    if blocking:
                                        # Keep the existing renderer alive so its
                                        # spinner keeps signalling background work
                                        # after we print this synchronous reply.
                                        with renderer.pause() if renderer else nullcontext():
                                            if renderer and renderer.header_printed:
                                                print_kwargs["show_header"] = False
                                            _print_agent_response(
                                                content,
                                                render_markdown=markdown,
                                                metadata=meta,
                                                **print_kwargs,
                                            )
                                    else:
                                        if renderer:
                                            await renderer.close()
                                        if renderer and renderer.header_printed:
                                            print_kwargs["show_header"] = False
                                        _print_agent_response(
                                            content,
                                            render_markdown=markdown,
                                            metadata=meta,
                                            **print_kwargs,
                                        )
                            elif renderer and not renderer.streamed and not blocking:
                                await renderer.close()
                            if not blocking:
                                break
                            turn_done.clear()
                            turn_response.clear()
                            await turn_done.wait()
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Show channel status \u2014 name, source (builtin / plugin), enabled."""
    from durin.channels.registry import discover_all, discover_channel_names
    from durin.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)
    builtin_names = set(discover_channel_names())

    table = Table(title="Channels")
    table.add_column("Channel", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        source = "builtin" if name in builtin_names else "plugin"
        table.add_row(
            cls.display_name,
            source,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(..., help="Channel name (e.g. weixin, whatsapp)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication even if already logged in"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Authenticate with a channel via QR code or other interactive login."""
    from durin.channels.registry import discover_all
    from durin.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)
    channel_cfg = getattr(config.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        available = ", ".join(all_channels.keys())
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {available}")
        raise typer.Exit(1)

    console.print(f"{__logo__} {all_channels[channel_name].display_name} Login\n")

    channel_cls = all_channels[channel_name]
    channel = channel_cls(channel_cfg, bus=None)

    success = asyncio.run(channel.login(force=force))

    if not success:
        raise typer.Exit(1)


# ============================================================================
# Plugin Commands
# ============================================================================

# `durin plugins list` was removed in alpha — its content (channel
# discovery list with builtin / plugin source) was 95% duplicated by
# `durin channels status`, which now carries the Source column too.


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    """Show what is set up AND what is running right now.

    ``status`` answers "what do I have and is it up?"; ``doctor`` answers
    "is anything broken (and fix it)?". status passes no judgement — it
    probes the live gateway for runtime state (version, uptime, channel
    connections, cron) and shows only the sections that have content so
    it stays a tight snapshot.
    """
    from durin.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    runtime = _probe_gateway_runtime(config)

    if as_json:
        import json as _json

        print(_json.dumps(_status_data(config, config_path, runtime), indent=2))
        return

    console.print(f"{__logo__} [bold]durin[/bold] · {__version__}\n")

    for label, value in _status_sections(config, config_path, runtime):
        console.print(f"  [cyan]{label:<10}[/cyan] {value}")


def _gateway_base_url(config: Any) -> str:
    """Base URL of the gateway HTTP surface (the websocket channel's port).

    This is the local PROBE address used to reach the gateway's own API —
    deliberately not the public_url-aware dashboard_url() resolver.
    """
    ws_section = getattr(config.channels, "websocket", None)
    host = "127.0.0.1"
    port = 8765  # websocket channel default
    if ws_section is not None:
        if isinstance(ws_section, dict):
            host = ws_section.get("host", host) or host
            port = ws_section.get("port", port) or port
        else:
            host = getattr(ws_section, "host", host) or host
            port = getattr(ws_section, "port", port) or port
    return f"http://{host}:{port}"


def _probe_gateway_runtime(config: Any, *, timeout: float = 1.5) -> dict[str, Any] | None:
    """Ask the live gateway what it is running. Returns ``None`` when it
    doesn't respond (not running, or bound elsewhere).

    Two calls: the unauthenticated ``/api/v1/health`` (version + uptime,
    enough to detect a stale install), then the authenticated
    ``/api/v1/status`` (channel runtime state + cron) using the websocket
    channel's static token. Auth failure degrades gracefully — the health
    half still renders.
    """
    base = _gateway_base_url(config)
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{base}/api/v1/health")
            if r.status_code != 200:
                return None
            health = r.json()
            out: dict[str, Any] = {
                "url": f"{base}/",
                "version": health.get("version"),
                "uptime_s": health.get("uptime_s"),
                "channels": None,
                "cron": None,
            }
            ws_section = getattr(config.channels, "websocket", None)
            token = (
                ws_section.get("token")
                if isinstance(ws_section, dict)
                else getattr(ws_section, "token", "")
            ) or ""
            if token:
                rs = client.get(
                    f"{base}/api/v1/status",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if rs.status_code == 200:
                    body = rs.json()
                    out["channels"] = body.get("channels")
                    out["cron"] = body.get("cron")
            return out
    except Exception:  # noqa: BLE001 — any network error means "not reachable"
        return None


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _status_data(
    config: Any, config_path: Path, runtime: dict[str, Any] | None
) -> dict[str, Any]:
    """Structured snapshot backing both the rendered rows and ``--json``."""
    from durin.cli.tui.startup import memory_summary
    from durin.config.loader import _is_split_layout
    from durin.providers.registry import PROVIDERS
    from durin.utils.oauth import any_token_present

    data: dict[str, Any] = {"version": __version__}

    # --- Model -------------------------------------------------------
    model, preset_tag = _model_display(config)
    d = config.agents.defaults
    data["model"] = {
        "model": model,
        "preset_tag": preset_tag,
        "provider": d.provider,
        "context_window_tokens": d.context_window_tokens,
    }
    aux = getattr(config.agents, "aux_models", None)
    aux_map: dict[str, str] = {}
    if aux is not None:
        if getattr(aux, "vision", None) is not None and aux.vision.model:
            aux_map["vision"] = aux.vision.model
        if getattr(aux, "audio", None) is not None and aux.audio.model:
            aux_map["audio"] = aux.audio.model
    data["aux_models"] = aux_map

    # --- Providers (only the configured ones) ------------------------
    configured: list[str] = []
    for spec in PROVIDERS:
        p = getattr(config.providers, spec.name, None)
        if p is None:
            continue
        if spec.is_oauth:
            if any_token_present(spec.name):
                configured.append(f"{spec.label} (OAuth)")
        elif spec.is_local:
            if getattr(p, "api_base", None):
                configured.append(spec.label)
        elif getattr(p, "api_key", None):
            configured.append(spec.label)
    data["providers"] = configured

    # --- Channels: config-enabled, overlaid with live runtime state ---
    runtime_channels = {
        c["name"]: c for c in (runtime or {}).get("channels") or [] if "name" in c
    }
    channels: list[dict[str, Any]] = []
    extra = getattr(config.channels, "__pydantic_extra__", None) or {}
    for name, section in extra.items():
        en = section.get("enabled") if isinstance(section, dict) else getattr(section, "enabled", False)
        if not en:
            continue
        port = section.get("port") if isinstance(section, dict) else getattr(section, "port", None)
        entry: dict[str, Any] = {"name": name, "enabled": True, "port": port}
        # running: True/False from the live gateway; None when unknown
        # (gateway down or token missing).
        rc = runtime_channels.get(name)
        entry["running"] = rc.get("running") if rc is not None else None
        channels.append(entry)
    data["channels"] = channels

    # --- Gateway -----------------------------------------------------
    gw_data: dict[str, Any] | None = None
    pid: int | None = None
    try:
        from durin.cli.gateway_daemon import daemon_status

        gw = daemon_status()
        if gw.state == "running":
            pid = gw.pid
    except Exception:  # noqa: BLE001
        pass
    if runtime is not None or pid is not None:
        gw_version = (runtime or {}).get("version")
        gw_data = {
            "running": runtime is not None or pid is not None,
            "pid": pid,
            "version": gw_version,
            "uptime_s": (runtime or {}).get("uptime_s"),
            "url": (runtime or {}).get("url") or _resolved_webui_url(),
            "stale": bool(gw_version) and gw_version != __version__,
        }
    data["gateway"] = gw_data

    # --- Webui access --------------------------------------------------
    webui_data: dict[str, Any] | None = None
    if getattr(config.gateway, "webui_enabled", False):
        from durin.utils.public_url import dashboard_url

        ws_section = getattr(config.channels, "websocket", None)

        def _ws_value(key: str) -> Any:
            if ws_section is None:
                return None
            if isinstance(ws_section, dict):
                return ws_section.get(key)
            return getattr(ws_section, key, None)

        # The webui login gate accepts token_issue_secret with PRECEDENCE
        # over the static token (`token_issue_secret or token` at bootstrap),
        # so show the value the login form actually accepts.
        tok = _ws_value("token_issue_secret") or _ws_value("token")
        if tok:
            # The stored config value may be a ${secret:NAME} reference (same
            # as any channel credential) — resolve it for display the same
            # way _resolve_section_secrets does at channel startup. A dangling
            # or invalid reference falls back to showing the raw value rather
            # than failing `status`.
            from durin.security.secrets import resolve_secret

            try:
                tok = resolve_secret(tok)
            except Exception:  # noqa: BLE001
                pass
        webui_data = {"dashboard_url": dashboard_url(config), "token": tok}
    data["webui"] = webui_data

    # --- Cron --------------------------------------------------------
    data["cron"] = (runtime or {}).get("cron")

    # --- Memory ------------------------------------------------------
    mem_data: dict[str, Any] | None = None
    try:
        stats = memory_summary(config.workspace_path)
        mem_data = {
            "docs": stats["memory_docs"],
            "sessions": stats["sessions"],
            "skills": stats["skills"],
            "vector": bool(getattr(config.memory, "enabled", False)),
            "embedding_model": (
                config.memory.embedding.model
                if getattr(config.memory, "enabled", False)
                else None
            ),
        }
    except Exception:  # noqa: BLE001
        pass
    data["memory"] = mem_data

    # --- Config ------------------------------------------------------
    data["config"] = {
        "path": str(config_path),
        "exists": config_path.exists(),
        "layout": "split" if _is_split_layout(config_path) else "single file",
    }
    data["workspace"] = str(config.workspace_path)
    return data


def _status_sections(
    config: Any, config_path: Path, runtime: dict[str, Any] | None
) -> list[tuple[str, str]]:
    """Render the (label, value) rows for ``durin status`` from the
    structured snapshot. Sections with no content are omitted so the
    output stays tight."""
    data = _status_data(config, config_path, runtime)
    rows: list[tuple[str, str]] = []

    # --- Model -------------------------------------------------------
    m = data["model"]
    ctx = f"{m['context_window_tokens']:,} ctx" if m["context_window_tokens"] else ""
    model_line = " · ".join(
        p for p in (f"{m['model']}{m['preset_tag']}", m["provider"], ctx) if p
    )
    rows.append(("Model", model_line))
    if data["aux_models"]:
        aux_bits = [f"{k}: {v}" for k, v in data["aux_models"].items()]
        rows.append(("", "[dim]" + " · ".join(aux_bits) + "[/dim]"))

    # --- Providers ----------------------------------------------------
    if data["providers"]:
        rows.append((
            "Providers",
            f"{', '.join(data['providers'])}  [dim]({len(data['providers'])} configured)[/dim]",
        ))
    else:
        rows.append(("Providers", "[dim]none configured — run `durin onboard`[/dim]"))

    # --- Channels: enabled + live state -------------------------------
    if data["channels"]:
        bits = []
        for ch in data["channels"]:
            label = f"{ch['name']}:{ch['port']}" if ch.get("port") else ch["name"]
            if ch["running"] is True:
                bits.append(f"{label} [green]✓[/green]")
            elif ch["running"] is False:
                bits.append(f"{label} [red]not running[/red]")
            else:
                bits.append(label)
        rows.append(("Channels", ", ".join(bits)))

    # --- Gateway -------------------------------------------------------
    gw = data["gateway"]
    if gw is not None:
        bits = [f"running (pid {gw['pid']})" if gw["pid"] else "running"]
        if gw["version"]:
            bits.append(f"v{gw['version']}")
        if gw["uptime_s"] is not None:
            bits.append(f"up {_format_uptime(gw['uptime_s'])}")
        if gw["url"]:
            bits.append(gw["url"])
        rows.append(("Gateway", " · ".join(bits)))
        if gw["stale"]:
            rows.append((
                "",
                f"[yellow]gateway v{gw['version']} ≠ CLI v{__version__} — "
                "`durin gateway restart` to pick up the new install[/yellow]",
            ))

    # --- Webui access ----------------------------------------------------
    webui = data["webui"]
    if webui is not None:
        rows.append(("Dashboard", webui["dashboard_url"]))
        if webui["token"]:
            rows.append(("Web token", str(webui["token"])))

    # --- Cron ----------------------------------------------------------
    cron = data["cron"]
    if cron and cron.get("jobs"):
        line = f"{cron['jobs']} job{'s' if cron['jobs'] != 1 else ''}"
        next_ms = cron.get("next_wake_at_ms")
        if next_ms:
            import time as _time

            delta = max(0, int(next_ms / 1000 - _time.time()))
            line += f" · next in {_format_uptime(delta) if delta >= 60 else f'{delta}s'}"
        if not cron.get("enabled", True):
            line += " · [yellow]scheduler off[/yellow]"
        rows.append(("Cron", line))

    # --- Memory --------------------------------------------------------
    mem = data["memory"]
    if mem is not None:
        if mem["vector"] and mem["embedding_model"]:
            short_model = mem["embedding_model"].rsplit("/", 1)[-1]
            vector_part = f"vector on ({short_model})"
        else:
            vector_part = "vector off"
        rows.append((
            "Memory",
            f"{mem['docs']} docs · {mem['sessions']} sessions · "
            f"{mem['skills']} skills · {vector_part}",
        ))

    # --- Config --------------------------------------------------------
    cfg = data["config"]
    rows.append((
        "Config",
        f"{cfg['path']} [dim]({cfg['layout']})[/dim]"
        if cfg["exists"]
        else "[red]missing — run `durin onboard`[/red]",
    ))
    rows.append(("Workspace", data["workspace"]))

    return rows


# ============================================================================
# OAuth Login
# ============================================================================

# Renamed from `provider` → `oauth` in alpha to avoid collision with
# `config.providers` (the LLM provider list). This group is *only*
# about OAuth-based providers (codex, copilot) — non-OAuth keys live in
# `durin config set providers.<vendor>.api_key …`.
oauth_app = typer.Typer(help="Sign in / out of OAuth-capable providers (codex, copilot, openrouter).")
app.add_typer(oauth_app, name="oauth")


_LOGIN_HANDLERS: dict[str, Callable[[], None]] = {}
_LOGOUT_HANDLERS: dict[str, Callable[[], None]] = {}

_PROVIDER_DISPLAY: dict[str, str] = {
    "openai_codex": "OpenAI Codex",
    "github_copilot": "GitHub Copilot",
    "openrouter": "OpenRouter",
}


def _register_login(name: str):
    """Register an OAuth login handler."""
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn

    return decorator


def _register_logout(name: str):
    """Register an OAuth logout handler."""
    def decorator(fn):
        _LOGOUT_HANDLERS[name] = fn
        return fn
    return decorator


def _resolve_oauth_provider(provider: str):
    """Resolve and validate an OAuth provider configuration.

    Valid targets are token-based OAuth providers (``spec.is_oauth``) plus
    api-key providers with a registered login handler — OpenRouter's OAuth
    just *obtains* a regular API key, so its spec is not ``is_oauth``.
    """
    from durin.providers.registry import PROVIDERS

    def _supported(s) -> bool:
        return s.is_oauth or s.name in _LOGIN_HANDLERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and _supported(s)), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if _supported(s))
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)
    return spec


from durin.utils.oauth import should_use_device_code  # noqa: E402


@oauth_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
    device: bool = typer.Option(False, "--device", help="Force device-code flow"),
    loopback: bool = typer.Option(False, "--loopback", help="Force loopback PKCE flow"),
):
    """Authenticate with an OAuth provider."""
    spec = _resolve_oauth_provider(provider)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    if spec.name == "openai_codex" and (device or loopback):
        force = "device" if device else "loopback"
        try:
            _codex_login_flow(force=force)
        except ImportError:
            console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
            raise typer.Exit(1) from None
        return

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)
    handler()


@oauth_app.command("logout")
def provider_logout(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Log out from an OAuth provider."""
    spec = _resolve_oauth_provider(provider)

    handler = _LOGOUT_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Logout not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Logout - {spec.label}\n")
    handler()


def _codex_login_flow(force: str | None) -> None:
    """force: 'device' | 'loopback' | None (auto-detect)."""
    from durin.providers import codex_device_auth as cda

    existing = cda.existing_codex_session()
    if existing is not None:
        who = existing.email or existing.plan or "cuenta existente"
        src = "Codex CLI" if existing.source == "codex-cli" else "durin"
        reuse = typer.confirm(
            f"Encontré una sesión de Codex ({who}, vía {src}). ¿Usarla?",
            default=True,
        )
        if reuse and existing.source == "durin":
            console.print(f"[green]✓ Usando la sesión existente[/green] [dim]{who}[/dim]")
            return
        # codex-cli source or decline: fall through to a fresh connect.

    use_device = force == "device" or (force != "loopback" and should_use_device_code())
    if use_device:
        token = cda.login_blocking(print_fn=lambda s: console.print(s))
    else:
        token = cda.login_loopback_blocking(print_fn=lambda s: console.print(s))
    if not (token and token.access):
        console.print("[red]✗ Authentication failed[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]"
    )


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        _codex_login_flow(force=None)
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1) from None


@_register_logout("openai_codex")
def _logout_openai_codex() -> None:
    """Clear local OAuth credentials for OpenAI Codex."""
    from durin.providers.codex_device_auth import disconnect

    # The token now lives in the secret store; disconnect() removes it (and any
    # legacy kit file), so a file-only deletion would no longer log the user out.
    label = _PROVIDER_DISPLAY["openai_codex"]
    if disconnect():
        console.print(f"[green]✓ Logged out from {label}[/green]")
    else:
        console.print(f"[yellow]! No local OAuth credentials found for {label}[/yellow]")


@_register_logout("github_copilot")
def _logout_github_copilot() -> None:
    """Clear local OAuth credentials for GitHub Copilot."""
    try:
        from durin.providers.github_copilot_provider import disconnect
    except ImportError:
        console.print("[red]GitHub Copilot provider unavailable. Ensure oauth-cli-kit is installed.[/red]")
        raise typer.Exit(1) from None

    # The token now lives in the secret store; disconnect() removes it (and any
    # legacy kit file), so a file-only deletion would no longer log the user out.
    label = _PROVIDER_DISPLAY["github_copilot"]
    if disconnect():
        console.print(f"[green]✓ Logged out from {label}[/green]")
    else:
        console.print(f"[yellow]! No local OAuth credentials found for {label}[/yellow]")


@_register_login("openrouter")
def _login_openrouter() -> None:
    """Loopback PKCE: the exchange yields a regular API key, stored like a
    manual paste (secret store + ``${secret:}`` ref in config)."""
    from durin.providers.openrouter_oauth import login_loopback_blocking

    try:
        login_loopback_blocking(print_fn=lambda s: console.print(s))
        console.print(
            "[green]✓ OpenRouter conectado[/green]  "
            "[dim]key guardada en providers.openrouter.api_key[/dim]"
        )
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1) from None


@_register_logout("openrouter")
def _logout_openrouter() -> None:
    """Forget the OpenRouter key (config ref + durin-managed secret)."""
    from durin.providers.openrouter_oauth import disconnect

    label = _PROVIDER_DISPLAY["openrouter"]
    if disconnect():
        console.print(f"[green]✓ Logged out from {label}[/green]")
    else:
        console.print(f"[yellow]! No stored key found for {label}[/yellow]")


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    try:
        from durin.providers.github_copilot_provider import login_github_copilot

        console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")
        token = login_github_copilot(
            print_fn=lambda s: console.print(s),
            prompt_fn=lambda s: typer.prompt(s),
        )
        account = token.account_id or "GitHub"
        console.print(f"[green]✓ Authenticated with GitHub Copilot[/green]  [dim]{account}[/dim]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1) from None


# Register lifecycle / diagnostic commands last so they sort below the
# everyday commands (onboard, agent, gateway, …) in `durin --help`.
app.add_typer(_config_app, name="config")
_register_upgrade(app)
_register_uninstall(app)
_register_doctor(app)

from durin.cli.secret_cmd import secret_app as _secret_app  # noqa: E402

app.add_typer(_secret_app, name="secret")

from durin.cli.memory_cmd import memory_app as _memory_app  # noqa: E402

app.add_typer(_memory_app, name="memory")

from durin.cli.skill_cmd import skill_app as _skill_app  # noqa: E402

app.add_typer(_skill_app, name="skill")

from durin.cli.workflow_cmd import workflow_app as _workflow_app  # noqa: E402

app.add_typer(_workflow_app, name="workflow")

from durin.cli.auth_cmd import auth_app as _auth_app  # noqa: E402

app.add_typer(_auth_app, name="auth")

from durin.cli.mcp_cmd import mcp_app as _mcp_app  # noqa: E402

app.add_typer(_mcp_app, name="mcp")


def _refresh_help_epilog() -> None:
    """Regenerate `durin --help`'s group listing from the live registry.

    Keeps the epilog honest: every command group and its subcommands
    are listed automatically, so adding a group never leaves the help
    stale. Falls back to the static `_HELP_EPILOG` on any introspection
    error (Typer internals differ across versions).
    """
    try:
        lines: list[str] = []
        for group in app.registered_groups:
            sub = group.typer_instance
            if sub is None or not group.name:
                continue
            names = sorted(
                (cmd.name or (cmd.callback.__name__ if cmd.callback else ""))
                for cmd in sub.registered_commands
            )
            names = [n for n in names if n]
            if names:
                lines.append(f"[bold]{group.name}[/bold] — {', '.join(names)}")
        if lines:
            app.info.epilog = (
                "Command groups (run `durin GROUP --help` for the full list):"
                "\n\n" + "\n\n".join(lines) + "\n\n" + _HELP_FOOTER
            )
    except Exception:  # noqa: BLE001
        pass  # keep the static _HELP_EPILOG


_refresh_help_epilog()


if __name__ == "__main__":
    app()
