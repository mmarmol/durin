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
