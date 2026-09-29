"""Tests for the shared inbound message deduplicator."""

from durin.channels.dedup import MessageDeduplicator


def test_first_sighting_is_not_duplicate():
    d = MessageDeduplicator()
    assert d.is_duplicate("m1") is False
    assert d.is_duplicate("m1") is True


def test_falsy_key_never_duplicate():
    d = MessageDeduplicator()
    assert d.is_duplicate("") is False
    assert d.is_duplicate("") is False


def test_ttl_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("durin.channels.dedup.time.time", lambda: now[0])
    d = MessageDeduplicator(ttl_seconds=300)
    assert d.is_duplicate("m1") is False
    now[0] += 301
    assert d.is_duplicate("m1") is False  # expired -> treated as new


def test_max_size_prunes_oldest(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("durin.channels.dedup.time.time", lambda: now[0])
    d = MessageDeduplicator(max_size=3, ttl_seconds=10_000)
    for i in range(4):
        now[0] += 1
        d.is_duplicate(f"m{i}")
    assert len(d._seen) <= 3
    assert d.is_duplicate("m3") is True  # newest survived the prune

