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


def test_reexec_flushes_the_log_sink_before_exiting_on_execv_failure(monkeypatch):
    """os._exit skips atexit hooks entirely, and the gateway's file sink
    queues writes (enqueue=True, durin/cli/gateway_logging.py) — without an
    explicit flush first, the ``logger.exception`` call recording the
    failure could be lost, the one record that would explain why the
    process is restarting cold instead of from a graceful shutdown."""
    from durin.utils import restart as restart_mod

    monkeypatch.setattr("durin.utils.restart.os.execv", MagicMock(side_effect=OSError("boom")))
    events: list[str] = []
    monkeypatch.setattr("durin.utils.restart.logger.complete", lambda: events.append("complete"))
    monkeypatch.setattr("durin.utils.restart.os._exit", lambda code: events.append(f"exit:{code}"))

    restart_mod.reexec()

    assert events == ["complete", "exit:1"]


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


def test_a_stuck_loser_does_not_hold_the_lock_for_a_later_reexec_call(monkeypatch):
    """The bug this guards: the loser used to decide-and-block inside
    ``with _reexec_lock:``, so it never released the lock. ``conftest.py``'s
    ``_reset_reexec_guard`` resets the flag between tests, but a loser
    thread stuck from an earlier test kept the module lock — so any LATER
    test's own call to the real ``reexec()`` hung forever just acquiring it
    (reproduced with ``pytest tests/utils tests/cli`` in that order; the
    default collection order only hid it). Simulate that exact sequence in
    one process: drive one call into losing and blocking forever, then make
    a second, independent call (as a later test's ``_reset_reexec_guard``
    would set up) and confirm it still completes instead of hanging on a
    stale lock."""
    import durin.utils.restart as restart_mod

    monkeypatch.setattr(restart_mod, "_reexeced", True)  # force the first call to lose
    first_call_started = threading.Event()

    def _loser() -> None:
        first_call_started.set()
        restart_mod.reexec()  # blocks forever if the lock-leak regresses

    loser_thread = threading.Thread(target=_loser, daemon=True)
    loser_thread.start()
    assert first_call_started.wait(timeout=1)

    # A later, independent restart attempt — the flag reset _reset_reexec_
    # guard performs between pytest tests, simulated here in the same
    # process while the first thread is still alive and blocked.
    monkeypatch.setattr(restart_mod, "_reexeced", False)
    exec_calls: list[bool] = []
    monkeypatch.setattr("durin.utils.restart.os.execv", lambda *a, **k: exec_calls.append(True))

    def _later_call() -> None:
        restart_mod.reexec()

    later_thread = threading.Thread(target=_later_call, daemon=True)
    later_thread.start()
    later_thread.join(timeout=2)

    assert not later_thread.is_alive(), (
        "a later reexec() call hung — the first loser's lock was still held"
    )
    assert exec_calls == [True]


def test_should_show_cli_restart_notice():
    notice = RestartNotice(channel="cli", chat_id="direct", started_at_raw="100")
    assert should_show_cli_restart_notice(notice, "cli:direct") is True
    assert should_show_cli_restart_notice(notice, "cli:other") is False
    assert should_show_cli_restart_notice(notice, "direct") is True

    non_cli = RestartNotice(channel="feishu", chat_id="oc_1", started_at_raw="100")
    assert should_show_cli_restart_notice(non_cli, "cli:direct") is False

