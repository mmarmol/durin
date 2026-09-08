from datetime import date
from pathlib import Path

from durin.memory.session_summary_store import (
    find_previous_session_summary,
    write_session_summary,
)


def test_previous_summary_is_the_newest_other_session_on_the_channel(tmp_path: Path) -> None:
    write_session_summary(tmp_path, "websocket:old", "- decided to use X", last_active=date(2026, 9, 1))
    write_session_summary(tmp_path, "websocket:older", "- ancient", last_active=date(2026, 8, 1))
    write_session_summary(tmp_path, "slack:c1", "- slack stuff", last_active=date(2026, 9, 5))

    found = find_previous_session_summary(tmp_path, "websocket:new", channels={"websocket", "cli"})

    assert found is not None
    stem, text, last_active = found
    assert stem == "websocket_old"
    assert "decided to use X" in text
    assert last_active == date(2026, 9, 1)


def test_previous_summary_skips_own_key_and_other_channels(tmp_path: Path) -> None:
    write_session_summary(tmp_path, "websocket:new", "- mine", last_active=date(2026, 9, 6))
    write_session_summary(tmp_path, "slack:c1", "- slack", last_active=date(2026, 9, 5))

    assert find_previous_session_summary(tmp_path, "websocket:new", channels={"websocket"}) is None
    assert find_previous_session_summary(tmp_path, "slack:c2", channels={"websocket"}) is None
