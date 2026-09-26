"""The inbound journal: messages the gateway still owed a turn to when it
stopped, written at shutdown and replayed at the next start."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from durin.bus.events import InboundMessage
from durin.bus.journal import InboundJournal


def _msg(content: str, **kwargs) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content=content, **kwargs)


def test_round_trip_preserves_message_fields_and_order(tmp_path: Path) -> None:
    journal = InboundJournal(tmp_path / "sessions" / ".inbound_journal.jsonl")
    first = _msg("first", media=["/tmp/a.png"], metadata={"message_id": "m1", "steer": True},
                 session_key_override="unified", is_dm=True)
    second = _msg("second")

    assert journal.append([first, second]) == 2
    drained = journal.drain()

    assert [m.content for m in drained] == ["first", "second"]
    got = drained[0]
    assert got.channel == "telegram" and got.sender_id == "u1" and got.chat_id == "c1"
    assert got.media == ["/tmp/a.png"]
    assert got.metadata == {"message_id": "m1", "steer": True}
    assert got.session_key_override == "unified"
    assert got.is_dm is True
    assert got.timestamp == first.timestamp


def test_drain_removes_the_file_so_a_message_is_replayed_once(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = InboundJournal(path)
    journal.append([_msg("once")])

    assert len(journal.drain()) == 1
    assert not path.exists()
    assert journal.drain() == []


def test_drain_skips_messages_older_than_the_age_cap(tmp_path: Path) -> None:
    journal = InboundJournal(tmp_path / "j.jsonl", max_age_s=3600)
    stale = _msg("stale", timestamp=datetime.now() - timedelta(hours=2))
    fresh = _msg("fresh")
    journal.append([stale, fresh])

    assert [m.content for m in journal.drain()] == ["fresh"]


def test_drain_skips_malformed_lines_and_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = InboundJournal(path)
    journal.append([_msg("good")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps({"channel": "telegram"}) + "\n")  # missing required fields

    assert [m.content for m in journal.drain()] == ["good"]


def test_drain_on_a_missing_file_returns_nothing(tmp_path: Path) -> None:
    assert InboundJournal(tmp_path / "absent.jsonl").drain() == []


def test_append_of_nothing_writes_no_file(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    assert InboundJournal(path).append([]) == 0
    assert not path.exists()


# ---------------------------------------------------------------------------
# Cross-process partitioning: one journal file is shared by the gateway, the
# TUI and the legacy REPL. A drain must only take the entries journaled by a
# process of its OWN kind, leaving another kind's entries in place.
# ---------------------------------------------------------------------------


def test_a_tui_written_entry_survives_a_gateway_replay_and_is_replayed_by_the_tui(
    tmp_path: Path,
) -> None:
    journal = InboundJournal(tmp_path / "j.jsonl")
    journal.append([_msg("from the tui")], kind="tui")

    # A gateway starting up must not steal the TUI's own turn.
    assert journal.drain(kind="gateway") == []

    # The TUI's own next start still finds it.
    replayed = journal.drain(kind="tui")
    assert [m.content for m in replayed] == ["from the tui"]
    assert journal.drain(kind="tui") == []       # replayed once


def test_the_gateways_own_entries_are_replayed(tmp_path: Path) -> None:
    journal = InboundJournal(tmp_path / "j.jsonl")
    journal.append([_msg("from the gateway")], kind="gateway")

    assert [m.content for m in journal.drain(kind="gateway")] == ["from the gateway"]


def test_each_kind_only_takes_its_own_entries_from_a_mixed_file(tmp_path: Path) -> None:
    journal = InboundJournal(tmp_path / "j.jsonl")
    journal.append([_msg("g1")], kind="gateway")
    journal.append([_msg("t1")], kind="tui")
    journal.append([_msg("g2")], kind="gateway")

    assert [m.content for m in journal.drain(kind="gateway")] == ["g1", "g2"]
    assert [m.content for m in journal.drain(kind="tui")] == ["t1"]
    assert journal.drain(kind="gateway") == []
    assert journal.drain(kind="tui") == []


def test_an_untagged_entry_matches_any_replaying_kind(tmp_path: Path) -> None:
    """A journal file written before this partitioning existed carries no
    kind tag at all; a reader that now asks for a specific kind must still
    take it, exactly as it always could."""
    path = tmp_path / "j.jsonl"
    journal = InboundJournal(path)
    journal.append([_msg("pre-upgrade")])   # no kind= at all — untagged

    assert [m.content for m in journal.drain(kind="gateway")] == ["pre-upgrade"]


def test_drain_with_no_kind_ignores_every_tag(tmp_path: Path) -> None:
    """A caller that does not distinguish process kinds at all (drain() with
    no kind=, matching every call before this feature existed) still takes
    everything, whatever it is tagged with."""
    journal = InboundJournal(tmp_path / "j.jsonl")
    journal.append([_msg("g")], kind="gateway")
    journal.append([_msg("t")], kind="tui")

    assert sorted(m.content for m in journal.drain()) == ["g", "t"]


def test_concurrent_append_during_a_drain_is_not_lost(tmp_path: Path, monkeypatch) -> None:
    """append and drain both lock the file: a concurrent append waits for a
    drain in progress to finish rather than racing its read-modify-write."""
    import threading
    from contextlib import contextmanager

    import durin.bus.journal as journal_mod

    path = tmp_path / "j.jsonl"
    journal = InboundJournal(path)
    journal.append([_msg("before")], kind="gateway")

    real_lock = journal_mod.cross_process_lock
    drain_holds_lock = threading.Event()
    release_drain = threading.Event()
    calls = {"n": 0}

    @contextmanager
    def _instrumented_lock(target, **kwargs):
        calls["n"] += 1
        first_call = calls["n"] == 1
        with real_lock(target, **kwargs):
            if first_call:
                drain_holds_lock.set()
                assert release_drain.wait(timeout=5), "drain never released"
            yield

    monkeypatch.setattr(journal_mod, "cross_process_lock", _instrumented_lock)

    drained: dict[str, list] = {}
    def _drain() -> None:
        drained["result"] = journal.drain(kind="gateway")

    drain_thread = threading.Thread(target=_drain)
    drain_thread.start()
    assert drain_holds_lock.wait(timeout=5), "drain never acquired the lock"

    appended = {}
    def _append() -> None:
        appended["count"] = journal.append([_msg("concurrent")], kind="gateway")

    append_thread = threading.Thread(target=_append)
    append_thread.start()

    release_drain.set()
    drain_thread.join(timeout=5)
    append_thread.join(timeout=5)

    assert [m.content for m in drained["result"]] == ["before"]
    assert appended["count"] == 1
    # Not lost: a later drain still finds the message the concurrent append wrote.
    assert [m.content for m in journal.drain(kind="gateway")] == ["concurrent"]
