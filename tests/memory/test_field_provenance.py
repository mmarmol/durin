from datetime import datetime, timedelta, timezone

import pytest

from durin.memory.field_provenance import incoming_wins, make_entry

NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=1)


def _e(author, at):
    return make_entry(source_ref="x", author=author, at=at)


def test_make_entry_shape():
    e = make_entry(source_ref="[[sessions/s.md#turn-3]]", author="agent",
                   at=datetime(2026, 6, 5, tzinfo=timezone.utc))
    assert e == {
        "source_ref": "[[sessions/s.md#turn-3]]",
        "extracted_at": "2026-06-05T00:00:00+00:00",
        "author": "agent",
    }


def test_make_entry_rejects_bad_author():
    with pytest.raises(ValueError):
        make_entry(source_ref="x", author="dream_bot", at=NOW)


def test_user_beats_dream_and_agent():
    assert incoming_wins(existing=_e("dream", NOW), incoming=_e("user", OLD)) is True
    assert incoming_wins(existing=_e("user", NOW), incoming=_e("dream", NOW)) is False


def test_dream_beats_agent():
    assert incoming_wins(existing=_e("agent", NOW), incoming=_e("dream", OLD)) is True


def test_same_level_recency_wins():
    assert incoming_wins(existing=_e("agent", OLD), incoming=_e("agent", NOW)) is True
    assert incoming_wins(existing=_e("agent", NOW), incoming=_e("agent", OLD)) is False


def test_no_existing_incoming_wins():
    assert incoming_wins(existing=None, incoming=_e("agent", NOW)) is True
