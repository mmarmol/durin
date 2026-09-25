"""Tests for restart notice helpers."""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock

from durin.utils.restart import (
    RestartNotice,
    consume_restart_notice_from_env,
    format_restart_completed_message,
    reexec,
    set_restart_notice_to_env,
    should_show_cli_restart_notice,
)


def test_set_and_consume_restart_notice_env_roundtrip(monkeypatch):
    monkeypatch.delenv("DURIN_RESTART_NOTIFY_CHANNEL", raising=False)
    monkeypatch.delenv("DURIN_RESTART_NOTIFY_CHAT_ID", raising=False)
    monkeypatch.delenv("DURIN_RESTART_NOTIFY_METADATA", raising=False)
    monkeypatch.delenv("DURIN_RESTART_STARTED_AT", raising=False)

    set_restart_notice_to_env(channel="feishu", chat_id="oc_123")

    notice = consume_restart_notice_from_env()
    assert notice is not None
    assert notice.channel == "feishu"
    assert notice.chat_id == "oc_123"
    assert notice.started_at_raw
    assert notice.metadata == {}

    # Consumed values should be cleared from env.
    assert consume_restart_notice_from_env() is None
    assert "DURIN_RESTART_NOTIFY_CHANNEL" not in os.environ
    assert "DURIN_RESTART_NOTIFY_CHAT_ID" not in os.environ
    assert "DURIN_RESTART_NOTIFY_METADATA" not in os.environ
    assert "DURIN_RESTART_STARTED_AT" not in os.environ


def test_restart_notice_preserves_metadata_across_env(monkeypatch):
    monkeypatch.delenv("DURIN_RESTART_NOTIFY_CHANNEL", raising=False)
    monkeypatch.delenv("DURIN_RESTART_NOTIFY_CHAT_ID", raising=False)
    monkeypatch.delenv("DURIN_RESTART_NOTIFY_METADATA", raising=False)
    monkeypatch.delenv("DURIN_RESTART_STARTED_AT", raising=False)

    set_restart_notice_to_env(
        channel="slack",
        chat_id="C123",
        metadata={"slack": {"thread_ts": "1700.42", "channel_type": "channel"}},
    )

    notice = consume_restart_notice_from_env()
    assert notice is not None
    assert notice.metadata == {
        "slack": {"thread_ts": "1700.42", "channel_type": "channel"}
    }
    assert "DURIN_RESTART_NOTIFY_METADATA" not in os.environ


def test_restart_notice_clears_stale_metadata(monkeypatch):
    monkeypatch.setenv("DURIN_RESTART_NOTIFY_METADATA", '{"stale": true}')
    set_restart_notice_to_env(channel="cli", chat_id="direct")
    assert "DURIN_RESTART_NOTIFY_METADATA" not in os.environ
    # set_restart_notice_to_env writes CHANNEL/CHAT_ID/STARTED_AT straight to
    # os.environ (not via monkeypatch), so they survive this test's teardown
    # and leak a "cli" restart notice into whichever test runs `durin agent`
    # next in the same process. Drain them the same way production does.
    consume_restart_notice_from_env()


def test_format_restart_completed_message_with_elapsed(monkeypatch):
    monkeypatch.setattr("durin.utils.restart.time.time", lambda: 102.0)
    assert format_restart_completed_message("100.0") == "Restart completed in 2.0s."


def test_reexec_exits_hard_when_execv_fails(monkeypatch):
    """A restart running on the watchdog's own thread that hits a failing
    os.execv must not just quietly end that thread — nothing else would
    ever retry it. It logs and exits hard so a supervisor (systemd,
    launchd) restarts the process instead of it running on with the
    restart request silently lost."""
    monkeypatch.setattr("durin.utils.restart.os.execv", MagicMock(side_effect=OSError("boom")))
    exit_calls: list[int] = []
    monkeypatch.setattr("durin.utils.restart.os._exit", exit_calls.append)

    reexec()

    assert exit_calls == [1]


def test_reexec_blocks_instead_of_returning_when_it_loses_the_race(monkeypatch):
    """The loser of the reexec race (the normal restart path or the
    watchdog, whichever calls second) must never return: returning could
    let this process reach its own exit before the winner's execv actually
    replaces it. Blocking is safe regardless — the winner's execv ends this
    thread along with everything else in the process the instant it
    succeeds."""
    monkeypatch.setattr("durin.utils.restart._reexeced", True)  # the winner "already" ran
    returned = threading.Event()

    def _call_reexec() -> None:
        reexec()
        returned.set()  # only reached if reexec() incorrectly returns

    thread = threading.Thread(target=_call_reexec, daemon=True)
    thread.start()
    thread.join(timeout=0.3)

    assert thread.is_alive(), "reexec() returned instead of blocking — the loser must never return"
    assert not returned.is_set()


def test_should_show_cli_restart_notice():
    notice = RestartNotice(channel="cli", chat_id="direct", started_at_raw="100")
    assert should_show_cli_restart_notice(notice, "cli:direct") is True
    assert should_show_cli_restart_notice(notice, "cli:other") is False
    assert should_show_cli_restart_notice(notice, "direct") is True

    non_cli = RestartNotice(channel="feishu", chat_id="oc_1", started_at_raw="100")
    assert should_show_cli_restart_notice(non_cli, "cli:direct") is False

