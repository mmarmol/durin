"""End-to-end wiring of `PushSink` onto the session telemetry logger.

Audit A8 (2026-05-28): when `cfg.telemetry.push.enabled` is true,
the logger fans events out to a PushSink alongside the JSONL local
write. The local persistence is ALWAYS primary; push is additive.

These tests exercise the BEHAVIOUR per
[[feedback-sync-tests-exercise-behavior]] — not just the TypedDict
or config schema, but that the wiring path actually creates the
sink, the bearer token comes from the secret store (not config
plaintext), and the logger fan-out reaches the push side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from durin.config.schema import TelemetryPushConfig
from durin.telemetry.logger import TelemetryLogger
from durin.telemetry.wiring import wire_push_sink


class _StubSecretEntry:
    def __init__(self, value: str) -> None:
        self.value = value


class _StubSecretStore:
    def __init__(self, entries: dict[str, str]) -> None:
        self._entries = {
            name: _StubSecretEntry(value) for name, value in entries.items()
        }

    def get(self, name: str) -> Any:
        return self._entries.get(name)


@pytest.fixture
def session_logger(tmp_path: Path) -> TelemetryLogger:
    return TelemetryLogger(tmp_path / "session.jsonl")


def _install_secret_store(
    monkeypatch: pytest.MonkeyPatch, entries: dict[str, str]
) -> _StubSecretStore:
    store = _StubSecretStore(entries)
    monkeypatch.setattr(
        "durin.security.secrets.get_secret_store",
        lambda: store,
    )
    return store


# ---------------------------------------------------------------------------
# disabled-path: default config = no push sink
# ---------------------------------------------------------------------------


def test_wire_returns_none_when_push_disabled(
    session_logger: TelemetryLogger,
) -> None:
    """Default config has push.enabled=False → wire_push_sink no-ops."""
    cfg = TelemetryPushConfig()
    assert cfg.enabled is False
    sink = wire_push_sink(session_logger, cfg)
    assert sink is None
    assert session_logger.extra_sinks == []


def test_wire_returns_none_when_push_config_is_none(
    session_logger: TelemetryLogger,
) -> None:
    """A loop with no telemetry config at all (e.g. tests that skip
    AgentLoop wiring) must not raise — push wiring is opt-in."""
    sink = wire_push_sink(session_logger, None)
    assert sink is None


# ---------------------------------------------------------------------------
# misconfigured: enabled=true but missing url/token_secret_name
# ---------------------------------------------------------------------------


def test_wire_returns_none_when_url_missing(
    session_logger: TelemetryLogger,
) -> None:
    cfg = TelemetryPushConfig(
        enabled=True, url=None, token_secret_name="X",
    )
    sink = wire_push_sink(session_logger, cfg)
    assert sink is None
    assert session_logger.extra_sinks == []


def test_wire_returns_none_when_secret_name_missing(
    session_logger: TelemetryLogger,
) -> None:
    cfg = TelemetryPushConfig(
        enabled=True, url="https://x.example.com/telemetry",
        token_secret_name=None,
    )
    sink = wire_push_sink(session_logger, cfg)
    assert sink is None
    assert session_logger.extra_sinks == []


# ---------------------------------------------------------------------------
# secret missing: graceful — push disabled, local JSONL keeps working
# ---------------------------------------------------------------------------


def test_wire_returns_none_when_secret_not_in_store(
    session_logger: TelemetryLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_secret_store(monkeypatch, {})  # empty store
    cfg = TelemetryPushConfig(
        enabled=True,
        url="https://x.example.com/telemetry",
        token_secret_name="DURIN_TELEMETRY_PUSH_TOKEN",
    )
    sink = wire_push_sink(session_logger, cfg)
    assert sink is None
    assert session_logger.extra_sinks == []


# ---------------------------------------------------------------------------
# happy path: enabled + url + secret resolves → push sink attached
# ---------------------------------------------------------------------------


def test_wire_attaches_push_sink_with_token_from_secret_store(
    session_logger: TelemetryLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bearer token comes from the secret store, NOT from config
    plaintext. The sink is registered on the logger so `log()` fans
    events to it after the JSONL write."""
    _install_secret_store(
        monkeypatch,
        {"DURIN_TELEMETRY_PUSH_TOKEN": "secret-token-value"},
    )
    cfg = TelemetryPushConfig(
        enabled=True,
        url="https://x.example.com/telemetry",
        token_secret_name="DURIN_TELEMETRY_PUSH_TOKEN",
        batch_size=5,
    )
    sink = wire_push_sink(session_logger, cfg)
    assert sink is not None
    assert sink.enabled is True
    # _token is private but we DO want to assert the secret reached
    # the sink — that's the key privacy invariant of this fixture.
    assert sink._token == "secret-token-value"  # noqa: SLF001
    assert sink._url == cfg.url  # noqa: SLF001
    assert sink._batch_size == 5  # noqa: SLF001
    assert session_logger.extra_sinks == [sink]


# ---------------------------------------------------------------------------
# behaviour: fan-out reaches PushSink alongside JSONL
# ---------------------------------------------------------------------------


def test_logger_fans_out_to_push_sink(
    session_logger: TelemetryLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a push sink is attached, every `logger.log(...)` call also
    reaches the sink. The JSONL file still receives the event."""
    _install_secret_store(
        monkeypatch, {"T": "tok"},
    )
    sink = wire_push_sink(
        session_logger,
        TelemetryPushConfig(
            enabled=True, url="https://x/", token_secret_name="T",
            batch_size=100,  # large batch → no auto-drain in test
        ),
    )
    assert sink is not None

    # Emit three events.
    session_logger.log("test.event", {"i": 1})
    session_logger.log("test.event", {"i": 2})
    session_logger.log("test.event", {"i": 3})

    # JSONL has them.
    lines = session_logger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3

    # Push sink buffered them (not yet drained because batch_size=100).
    assert sink.pending_count() == 3


def test_jsonl_keeps_working_when_push_sink_raises(
    session_logger: TelemetryLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per [[feedback-optimization-vs-principle]] — privacy/observability
    must not depend on a remote endpoint. A broken sink does NOT
    break the local JSONL persistence."""

    class _BrokenSink:
        def log(self, event_type: str, data: dict) -> None:
            raise RuntimeError("simulated push transport failure")

    session_logger.add_sink(_BrokenSink())
    # Logging must still write to JSONL despite the sink raising.
    session_logger.log("test.event", {"alive": True})
    lines = session_logger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "alive" in lines[0]


# ---------------------------------------------------------------------------
# privacy: schema does NOT carry the bearer token
# ---------------------------------------------------------------------------


def test_config_schema_has_no_plaintext_token_field() -> None:
    """The config carries `token_secret_name` (reference), NEVER the
    token value itself. A regression that adds a `token: str` field
    to TelemetryPushConfig would defeat the privacy contract."""
    fields = TelemetryPushConfig.model_fields
    assert "token_secret_name" in fields
    assert "token" not in fields, (
        "TelemetryPushConfig must NOT have a `token` field — the "
        "secret value lives in the secret store, not config.json. "
        "Use `token_secret_name` instead."
    )
