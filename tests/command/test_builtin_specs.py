"""Registry surface/visibility contract for builtin slash commands."""

from durin.command.builtin import (
    BUILTIN_COMMAND_SPECS,
    builtin_command_palette,
    channel_menu_commands,
    specs_for_surface,
)


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
