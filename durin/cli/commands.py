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
from durin.cli.stream import StreamRenderer, ThinkingSpinner
from durin.config.paths import get_workspace_path, is_default_workspace
from durin.config.schema import Config
from durin.utils.helpers import sync_workspace_templates
from durin.utils.restart import (
    consume_restart_notice_from_env,
    format_restart_completed_message,
    should_show_cli_restart_notice,
)

# Shown at the bottom of `durin --help`. The top-level listing only
# gives one line per command and never reveals what lives *inside* the
# command groups (`config`, `gateway`, …) — so spell those out here.
_HELP_EPILOG = (
    "Command groups (run `durin GROUP --help` for the full list):\n\n"
    "[bold]config[/bold] — path, show, get, set, edit\n\n"
    "[bold]gateway[/bold] — start, stop, restart, status, logs\n\n"
    "[bold]channels[/bold] — status, login\n\n"
    "[bold]oauth[/bold] — login, logout\n\n"
    "First run: `durin onboard`  ·  Health check: `durin doctor`  ·  "
    "Chat: `durin agent`"
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
    # via their shell (see docs/INSTALL.md).
    add_completion=False,
)

# D6 lifecycle commands: config get/set/show/edit/path, upgrade, uninstall.
# Registered at the end of the module (see bottom of file) so the
# everyday commands (onboard, agent, gateway, …) sort above the
# lifecycle/diagnostic ones in `durin --help`.
from durin.cli.config_cmd import config_app as _config_app  # noqa: E402
from durin.cli.upgrade import register as _register_upgrade  # noqa: E402
from durin.cli.uninstall import register as _register_uninstall  # noqa: E402
from durin.cli.doctor import register as _register_doctor  # noqa: E402

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
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.document import Document as _PtkDocument

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
            raise typer.Exit(1)
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
            raise typer.Exit(1)
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
        console.print(f"  1. Verify: [cyan]durin doctor[/cyan]")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  3. (optional) Chat apps: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")


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
        raise typer.Exit(1)
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
        raise typer.Exit(1)

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
    bus = MessageBus()
    session_manager = SessionManager(runtime_config.workspace_path)
    try:
        agent_loop = AgentLoop.from_config(
            runtime_config, bus,
            session_manager=session_manager,
            image_generation_provider_configs={
                "openrouter": runtime_config.providers.openrouter,
                "aihubmix": runtime_config.providers.aihubmix,
            },
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
            raise typer.Exit(1)
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
    ws_section = getattr(cfg.channels, "websocket", None)
    host = "127.0.0.1"
    port = 8765  # websocket channel default
    if ws_section is not None:
        if isinstance(ws_section, dict):
            host = ws_section.get("host", host)
            port = ws_section.get("port", port)
        else:
            host = getattr(ws_section, "host", host) or host
            port = getattr(ws_section, "port", port) or port
    return f"http://{host}:{port}/"


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
        raise typer.Exit(1)
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
    from durin.heartbeat.service import HeartbeatService
    from durin.providers.factory import build_provider_snapshot, load_provider_snapshot
    from durin.session.manager import SessionManager

    port = port if port is not None else config.gateway.port

    console.print(f"{__logo__} Starting durin gateway version {__version__} on port {port}...")
    sync_workspace_templates(config.workspace_path)
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
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop.from_config(
        config, bus,
        provider=provider_snapshot.provider,
        model=provider_snapshot.model,
        context_window_tokens=provider_snapshot.context_window_tokens,
        cron_service=cron,
        session_manager=session_manager,
        image_generation_provider_configs={
            "openrouter": config.providers.openrouter,
            "aihubmix": config.providers.aihubmix,
        },
        provider_snapshot_loader=load_provider_snapshot,
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
        # Dream is an internal job — run directly, not through the agent loop.
        if job.name == "dream":
            try:
                await agent.dream.run()
                logger.info("Dream cron job completed")
            except Exception:
                logger.exception("Dream cron job failed")
            return None

        from durin.utils.evaluator import evaluate_response

        reminder_note = (
            "The scheduled time has arrived. Deliver this reminder to the user now, "
            "as a brief and natural message in their language. Speak directly to them — "
            "do not narrate progress, summarize, include user IDs, or add status reports "
            "like 'Done' or 'Reminded'.\n\n"
            f"Reminder: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)

        async def _silent(*_args, **_kwargs):
            pass

        message_record_token = None
        if isinstance(message_tool, MessageTool):
            message_record_token = message_tool.set_record_channel_delivery(True)

        try:
            resp = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
                on_progress=_silent,
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
                response, reminder_note, agent.provider, agent.model,
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

    def _webui_runtime_model_name() -> str | None:
        model = getattr(agent, "model", None)
        if isinstance(model, str):
            stripped = model.strip()
            return stripped or None
        return None

    # Create channel manager (forwards SessionManager so the WebSocket channel
    # can serve the embedded webui's REST surface).
    channels = ChannelManager(
        config,
        bus,
        session_manager=session_manager,
        webui_runtime_model_name=_webui_runtime_model_name,
    )

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    heartbeat_preamble = (
        "[Your response will be delivered directly to the user's messaging app. "
        "Output ONLY the final user-facing message. Never reference internal "
        "files (HEARTBEAT.md, AWARENESS.md, etc.), your instructions, or your "
        "decision process. If nothing needs reporting, respond with just "
        "'All clear.' and nothing else.]\n\n"
    )

    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop.

        Two modes:
        - **Default (shared session)**: reuse ``session_key="heartbeat"``
          and trim via ``retain_recent_legal_suffix`` after the tick.
          Preserves short-term context between ticks.
        - **Isolated** (``heartbeat.isolatedSessions=true``,
          OpenClaw-inspired): fresh ephemeral session per tick, deleted
          after the tick. Stateless one-shots, no drift from prior runs.
        """
        from durin.heartbeat.service import heartbeat_session_key

        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        session_key = heartbeat_session_key(isolated=hb_cfg.isolated_sessions)

        resp = await agent.process_direct(
            heartbeat_preamble + tasks,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

        if hb_cfg.isolated_sessions:
            # Drop the ephemeral session from cache + disk so no state
            # carries over to the next tick.
            try:
                agent.sessions.delete_session(session_key)
            except Exception:
                logger.exception(
                    "Failed to clean up isolated heartbeat session {}",
                    session_key,
                )
        else:
            # Keep a small tail of heartbeat history so the loop stays
            # bounded without losing all short-term context between runs.
            session = agent.sessions.get_or_create(session_key)
            session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
            agent.sessions.save(session)

        return resp.content if resp else ""

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel.

        In addition to publishing the outbound message, this injects the
        delivered text as an assistant turn into the *target channel's*
        session.  Without this, a user reply on the channel (e.g. "Sure")
        lands in a session that has no context about the heartbeat message
        and the agent cannot follow through.
        """
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to

        await _deliver_to_channel(
            OutboundMessage(channel=channel, chat_id=chat_id, content=response),
            record=True,
        )

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=agent.provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        timezone=config.agents.defaults.timezone,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

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
    # Register Dream system job (always-on, idempotent on restart)
    dream_cfg = config.agents.defaults.dream
    if dream_cfg.model_override:
        agent.dream.model = dream_cfg.model_override
    agent.dream.max_batch_size = dream_cfg.max_batch_size
    agent.dream.max_iterations = dream_cfg.max_iterations
    agent.dream.annotate_line_ages = dream_cfg.annotate_line_ages
    from durin.cron.types import CronJob, CronPayload
    cron.register_system_job(CronJob(
        id="dream",
        name="dream",
        schedule=dream_cfg.build_schedule(config.agents.defaults.timezone),
        payload=CronPayload(kind="system_event"),
    ))
    console.print(f"[green]✓[/green] Dream: {dream_cfg.describe_schedule()}")

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

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            tasks = [
                agent.run(),
                channels.start_all(),
                _health_server(config.gateway.host, port),
            ]
            if open_browser_url:
                tasks.append(_open_browser_when_ready())
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback

            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            heartbeat.stop()
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

    bus = MessageBus()

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

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
                        from durin.cli.dragdrop import process_dragged_paths

                        clean_input, media_paths = process_dragged_paths(
                            user_input, agent_loop.workspace
                        )

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
def status():
    """Show durin's current configuration — a factual snapshot.

    ``status`` answers "what is set up?"; ``doctor`` answers "is
    anything broken?". status passes no judgement — it shows only the
    sections that have content (configured providers, enabled
    channels, …) so it stays readable instead of dumping every
    possible provider.
    """
    from durin.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()

    console.print(f"{__logo__} [bold]durin[/bold] · {__version__}\n")

    for label, value in _status_sections(config, config_path):
        console.print(f"  [cyan]{label:<10}[/cyan] {value}")


def _status_sections(config: Any, config_path: Path) -> list[tuple[str, str]]:
    """Build the (label, value) rows for ``durin status``. Sections with
    no content are omitted so the output stays a tight snapshot."""
    from durin.providers.registry import PROVIDERS
    from durin.utils.oauth import any_token_present

    rows: list[tuple[str, str]] = []

    # --- Model -------------------------------------------------------
    model, preset_tag = _model_display(config)
    d = config.agents.defaults
    ctx = f"{d.context_window_tokens:,} ctx" if d.context_window_tokens else ""
    model_line = " · ".join(p for p in (f"{model}{preset_tag}", d.provider, ctx) if p)
    rows.append(("Model", model_line))
    aux = getattr(config.agents, "aux_models", None)
    aux_bits = []
    if aux is not None:
        if getattr(aux, "vision", None) is not None and aux.vision.model:
            aux_bits.append(f"vision: {aux.vision.model}")
        if getattr(aux, "audio", None) is not None and aux.audio.model:
            aux_bits.append(f"audio: {aux.audio.model}")
    if aux_bits:
        rows.append(("", "[dim]" + " · ".join(aux_bits) + "[/dim]"))

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
    if configured:
        rows.append((
            "Providers",
            f"{', '.join(configured)}  [dim]({len(configured)} configured)[/dim]",
        ))
    else:
        rows.append(("Providers", "[dim]none configured — run `durin onboard`[/dim]"))

    # --- Channels (only the enabled ones) ----------------------------
    enabled_channels: list[str] = []
    channels_obj = config.channels
    extra = getattr(channels_obj, "__pydantic_extra__", None) or {}
    for name, section in extra.items():
        en = section.get("enabled") if isinstance(section, dict) else getattr(section, "enabled", False)
        if en:
            port = section.get("port") if isinstance(section, dict) else getattr(section, "port", None)
            enabled_channels.append(f"{name}:{port}" if port else name)
    if enabled_channels:
        rows.append(("Channels", ", ".join(enabled_channels)))

    # --- Gateway -----------------------------------------------------
    try:
        from durin.cli.gateway_daemon import daemon_status

        gw = daemon_status()
        if gw.state == "running":
            url = _resolved_webui_url() or ""
            rows.append(("Gateway", f"running (pid {gw.pid}){'  ' + url if url else ''}"))
    except Exception:  # noqa: BLE001
        pass

    # --- Memory ------------------------------------------------------
    try:
        ws = config.workspace_path
        mem_dir = ws / "memory"
        docs = len(list(mem_dir.glob("**/*.md"))) if mem_dir.exists() else 0
        sess_dir = ws / "sessions"
        sessions = len(list(sess_dir.glob("*.jsonl"))) if sess_dir.exists() else 0
        rows.append(("Memory", f"{docs} docs · {sessions} sessions"))
    except Exception:  # noqa: BLE001
        pass

    # --- Config ------------------------------------------------------
    from durin.config.loader import _is_split_layout

    layout = "split" if _is_split_layout(config_path) else "single file"
    rows.append((
        "Config",
        f"{config_path} [dim]({layout})[/dim]"
        if config_path.exists()
        else "[red]missing — run `durin onboard`[/red]",
    ))
    rows.append(("Workspace", str(config.workspace_path)))

    return rows


# ============================================================================
# OAuth Login
# ============================================================================

# Renamed from `provider` → `oauth` in alpha to avoid collision with
# `config.providers` (the LLM provider list). This group is *only*
# about OAuth-based providers (codex, copilot) — non-OAuth keys live in
# `durin config set providers.<vendor>.api_key …`.
oauth_app = typer.Typer(help="Sign in / out of OAuth-based providers (codex, copilot).")
app.add_typer(oauth_app, name="oauth")


_LOGIN_HANDLERS: dict[str, Callable[[], None]] = {}
_LOGOUT_HANDLERS: dict[str, Callable[[], None]] = {}

_PROVIDER_DISPLAY: dict[str, str] = {
    "openai_codex": "OpenAI Codex",
    "github_copilot": "GitHub Copilot",
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
    """Resolve and validate an OAuth provider configuration."""
    from durin.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)
    return spec


@oauth_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    spec = _resolve_oauth_provider(provider)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
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


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive

        token = None
        with suppress(Exception):
            token = get_token()
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_logout("openai_codex")
def _logout_openai_codex() -> None:
    """Clear local OAuth credentials for OpenAI Codex."""
    try:
        from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
        from oauth_cli_kit.storage import FileTokenStorage
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)

    storage = FileTokenStorage(token_filename=OPENAI_CODEX_PROVIDER.token_filename)
    _delete_oauth_files(storage.get_token_path(), _PROVIDER_DISPLAY["openai_codex"])


@_register_logout("github_copilot")
def _logout_github_copilot() -> None:
    """Clear local OAuth credentials for GitHub Copilot."""
    try:
        from durin.providers.github_copilot_provider import get_storage
    except ImportError:
        console.print("[red]GitHub Copilot provider unavailable. Ensure oauth-cli-kit is installed.[/red]")
        raise typer.Exit(1)

    storage = get_storage()
    _delete_oauth_files(storage.get_token_path(), _PROVIDER_DISPLAY["github_copilot"])


def _delete_oauth_files(token_path: Path, provider_label: str) -> None:
    """Delete OAuth token and lock files, reporting the result."""
    removed_paths: list[Path] = []
    skipped: list[tuple[Path, OSError]] = []
    for path in (token_path, token_path.with_suffix(".lock")):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            skipped.append((path, exc))
            continue
        removed_paths.append(path)

    if not removed_paths and not skipped:
        console.print(f"[yellow]! No local OAuth credentials found for {provider_label}[/yellow]")
        return

    if removed_paths:
        console.print(f"[green]✓ Logged out from {provider_label}[/green]")
        for path in removed_paths:
            console.print(f"[dim]Removed: {path}[/dim]")
    for path, exc in skipped:
        console.print(f"[yellow]! Could not remove {path}: {exc}[/yellow]")


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
        raise typer.Exit(1)


# Register lifecycle / diagnostic commands last so they sort below the
# everyday commands (onboard, agent, gateway, …) in `durin --help`.
app.add_typer(_config_app, name="config")
_register_upgrade(app)
_register_uninstall(app)
_register_doctor(app)


if __name__ == "__main__":
    app()
