"""Registry surface/visibility contract for builtin slash commands."""

from durin.command.builtin import (
    BUILTIN_COMMAND_SPECS,
    build_help_text,
    builtin_command_palette,
    channel_menu_commands,
    register_builtin_commands,
    specs_for_surface,
)
from durin.command.router import CommandRouter


def _spec(command: str):
    return next(s for s in BUILTIN_COMMAND_SPECS if s.command == command)


def test_admin_commands_never_listed():
    for surface in ("webui", "tui", "channels"):
        listed = {s.command for s in specs_for_surface(surface)}
        assert "/restart" not in listed
        assert "/version" not in listed


def test_webui_palette_excludes_terminal_and_channel_only_commands():
    listed = {row["command"] for row in builtin_command_palette("webui")}
    for cmd in ("/copy", "/hotkeys", "/history", "/stop", "/restart", "/version"):
        assert cmd not in listed
    for cmd in ("/new", "/model", "/persona", "/effort", "/compact", "/memory"):
        assert cmd in listed


def test_tui_surface_lists_terminal_commands():
    listed = {s.command for s in specs_for_surface("tui")}
    assert {"/copy", "/hotkeys", "/stop"} <= listed


def test_channels_surface_lists_history_and_stop():
    listed = {s.command for s in specs_for_surface("channels")}
    assert {"/history", "/stop", "/compact", "/persona"} <= listed


def test_channel_menu_names_are_telegram_safe():
    for name, title in channel_menu_commands():
        assert name == name.lower()
        assert "/" not in name and "-" not in name
        assert title


def test_effort_has_a_spec():
    spec = _spec("/effort")
    assert spec.arg_hint


def test_every_spec_command_has_a_registered_handler():
    router = CommandRouter()
    register_builtin_commands(router)
    for spec in BUILTIN_COMMAND_SPECS:
        cmd = spec.command
        registered = (
            cmd in router._priority
            or cmd in router._exact
            or any(pfx.strip() == cmd for pfx, _ in router._prefix)
        )
        assert registered, f"{cmd} is listed in SPECS but has no handler"


def test_every_registered_command_has_a_spec():
    router = CommandRouter()
    register_builtin_commands(router)
    spec_cmds = {s.command for s in BUILTIN_COMMAND_SPECS}
    registered = set(router._priority) | set(router._exact) | {
        pfx.strip() for pfx, _ in router._prefix
    }
    assert registered <= spec_cmds, f"handlers without spec: {registered - spec_cmds}"


def test_help_text_is_surface_scoped_and_hides_admin():
    channels_help = build_help_text("channels")
    assert "/history" in channels_help
    assert "/copy" not in channels_help
    assert "/restart" not in channels_help
    tui_help = build_help_text("tui")
    assert "/copy" in tui_help
