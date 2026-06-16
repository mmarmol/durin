"""Tests for durin.cli.sessions — most-recent + listing + age formatting."""

from __future__ import annotations

import time
from pathlib import Path

from durin.cli.sessions import (
    SessionInfo,
    fresh_session_id,
    list_sessions,
    most_recent_session,
)


def _make_session(workspace: Path, name: str, *, mtime_offset: float = 0.0, lines: int = 0) -> Path:
    sess_dir = workspace / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    path = sess_dir / f"{name}.jsonl"
    path.write_text("\n".join("{}" for _ in range(lines)) + ("\n" if lines else ""))
    if mtime_offset:
        # Set mtime backwards by `mtime_offset` seconds.
        ts = time.time() - mtime_offset
        import os

        os.utime(path, (ts, ts))
    return path


def test_list_sessions_empty_when_dir_missing(tmp_path: Path) -> None:
    assert list_sessions(tmp_path) == []


def test_list_sessions_returns_newest_first(tmp_path: Path) -> None:
    _make_session(tmp_path, "cli_oldest", mtime_offset=86400)  # 1 day ago
    _make_session(tmp_path, "cli_middle", mtime_offset=3600)   # 1 hour ago
    _make_session(tmp_path, "cli_newest", mtime_offset=0)      # now
    sessions = list_sessions(tmp_path)
    assert [s.chat_id for s in sessions] == ["newest", "middle", "oldest"]


def test_list_sessions_skips_non_underscore_names(tmp_path: Path) -> None:
    """Files without `<channel>_<chat_id>` shape are ignored."""
    _make_session(tmp_path, "weird-name")
    _make_session(tmp_path, "cli_good")
    sessions = list_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].chat_id == "good"


def test_session_info_msg_count_matches_jsonl_lines(tmp_path: Path) -> None:
    _make_session(tmp_path, "cli_chatA", lines=5)
    [info] = list_sessions(tmp_path)
    assert info.msg_count == 5


def test_most_recent_session(tmp_path: Path) -> None:
    _make_session(tmp_path, "cli_old", mtime_offset=86400)
    _make_session(tmp_path, "cli_new", mtime_offset=10)
    info = most_recent_session(tmp_path)
    assert info is not None
    assert info.chat_id == "new"


def test_most_recent_session_none_when_empty(tmp_path: Path) -> None:
    assert most_recent_session(tmp_path) is None


def test_age_label_seconds(tmp_path: Path) -> None:
    _make_session(tmp_path, "cli_a", mtime_offset=15)
    info = most_recent_session(tmp_path)
    assert info is not None
    assert info.age_label.endswith("s ago")


def test_age_label_hours(tmp_path: Path) -> None:
    _make_session(tmp_path, "cli_a", mtime_offset=7200)
    info = most_recent_session(tmp_path)
    assert info is not None
    assert info.age_label.endswith("h ago")
    assert info.age_label.startswith("2")


def test_age_label_days(tmp_path: Path) -> None:
    _make_session(tmp_path, "cli_a", mtime_offset=86400 * 3)
    info = most_recent_session(tmp_path)
    assert info is not None
    assert info.age_label.endswith("d ago")


def test_fresh_session_id_returns_cli_with_timestamp() -> None:
    channel, chat_id = fresh_session_id()
    assert channel == "cli"
    # Should look like '20260521_124930'.
    assert len(chat_id) == 15
    assert chat_id[8] == "_"


def test_session_info_key_is_channel_chat() -> None:
    info = SessionInfo(channel="cli", chat_id="alpha", path=Path("/tmp"), mtime=0, msg_count=0)
    assert info.key == "cli:alpha"
