"""Telegram slash-command surface: derived menu + generic forwarding."""

from durin.channels.telegram import TelegramChannel


def test_bot_commands_derive_from_registry():
    names = [c.command for c in TelegramChannel.bot_commands()]
    assert names[0] == "start"
    assert "compact" in names       # previously missing from the menu
    assert "persona" in names
    assert "effort" in names
    assert "usage" in names
    # Phantoms and admin commands are gone from the menu.
    for gone in ("dream", "dream_log", "dream_restore", "pairing", "restart"):
        assert gone not in names


def test_generic_slash_regex_forwards_any_command():
    pattern = TelegramChannel.TELEGRAM_BUS_SLASH_COMMAND_RE
    for text in ("/compact", "/sessions foo", "/pairing approve AB-CD", "/model@mybot gpt"):
        assert pattern.match(text), text
    assert not pattern.match("not a command")
    assert not pattern.match("/start")  # start keeps its dedicated handler


def test_normalize_maps_underscores_to_hyphenated_canonicals():
    # No hyphenated canonical commands exist today; the map must be empty and
    # normalization a pass-through (regression pin for the dream_* removal).
    assert TelegramChannel._normalize_telegram_command("/status") == "/status"
    assert TelegramChannel._normalize_telegram_command("plain text") == "plain text"
