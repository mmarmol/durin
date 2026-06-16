"""Opt-in HTTPS push of telemetry events (P7.3 / doc 07 §10).

The push runs alongside the local JSONL persistence — it's an
additional sink, not a replacement. When the configured URL or
token is missing, the push is a no-op (the user hasn't opted in).
Events are batched by count or wall-clock; the buffer drains
opportunistically.
"""

from __future__ import annotations

from typing import Any

from durin.telemetry.push import (
    DEFAULT_BATCH_SIZE,
    PushSink,
)


def test_default_batch_size() -> None:
    assert DEFAULT_BATCH_SIZE == 10


def test_disabled_when_url_missing() -> None:
    sink = PushSink(url="", token="t")
    assert not sink.enabled
    # log() is a no-op.
    sink.log("memory.recall", {"q": "x"})
    assert sink.pending_count() == 0


def test_disabled_when_token_missing() -> None:
    sink = PushSink(url="https://example.com/api", token="")
    assert not sink.enabled


def test_buffers_until_batch_size() -> None:
    sink = PushSink(
        url="https://example.com/api", token="t", batch_size=3,
    )
    sink.log("e1", {})
    sink.log("e2", {})
    assert sink.pending_count() == 2
    # Third event reaches batch_size → drain happens.
    captured: list[Any] = []

    def fake_post(url, json, headers):
        captured.append((url, json, headers))
        return _FakeResponse(204)

    sink._post = fake_post  # type: ignore[assignment]
    sink.log("e3", {})
    assert sink.pending_count() == 0
    assert len(captured) == 1
    posted_payload = captured[0][1]
    assert posted_payload["events"]
    assert len(posted_payload["events"]) == 3


def test_flush_drains_remaining() -> None:
    sink = PushSink(
        url="https://example.com/api", token="t", batch_size=100,
    )
    sink.log("e1", {})
    sink.log("e2", {})
    posted: list = []
    sink._post = lambda u, json, headers: (
        posted.append(json) or _FakeResponse(204)
    )
    sink.flush()
    assert sink.pending_count() == 0
    assert posted and len(posted[0]["events"]) == 2


def test_post_failure_keeps_events_for_retry() -> None:
    """A failed POST puts the events back so the next flush retries."""
    sink = PushSink(
        url="https://example.com/api", token="t", batch_size=2,
    )

    def failing_post(url, json, headers):
        raise RuntimeError("network down")

    sink._post = failing_post  # type: ignore[assignment]
    sink.log("e1", {})
    sink.log("e2", {})  # triggers drain → fails → events retained
    assert sink.pending_count() == 2


def test_authentication_header_present() -> None:
    sink = PushSink(
        url="https://example.com/api", token="secret-token",
        batch_size=1,
    )
    captured_headers: dict = {}

    def fake_post(url, json, headers):
        captured_headers.update(headers)
        return _FakeResponse(204)

    sink._post = fake_post  # type: ignore[assignment]
    sink.log("e1", {})
    assert captured_headers.get("Authorization") == "Bearer secret-token"


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")
