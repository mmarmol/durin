"""A dangling ${secret:} reference must be loud, not a buried warning.

The gateway keeps running when one channel cannot be built — correct, one bad
channel should not take the rest down. But the failure is invisible from
outside: the process is up and the other channels connect. When the casualty
is the websocket channel it takes the dashboard and the HTTP API with it, i.e.
the surface an operator would use to notice, and the only trace was a WARNING.
"""

from __future__ import annotations

import logging

from loguru import logger

from durin.channels.manager import ChannelManager
from durin.config.schema import Config
from durin.security.secrets import SecretNotFoundError


def _manager_raising(exc: Exception, monkeypatch) -> ChannelManager:
    from durin.channels import registry

    monkeypatch.setattr(
        registry, "discover_all",
        lambda: {"websocket": type("C", (), {"display_name": "WS"})},
    )
    m = ChannelManager.__new__(ChannelManager)
    m.config = Config()
    m.channels = {}
    m.config.channels.websocket = {"enabled": True, "token": "${secret:GONE}"}

    def _boom(_name):
        raise exc

    m._make_channel = _boom  # type: ignore[method-assign]
    m._validate_allow_from = lambda: None  # type: ignore[method-assign]
    m._ensure_channel_extras = lambda: None  # type: ignore[method-assign]
    return m


def _capture(caplog, fn) -> list[logging.LogRecord]:
    handler_id = logger.add(caplog.handler, format="{message}", level="WARNING")
    try:
        with caplog.at_level(logging.WARNING):
            fn()
    finally:
        logger.remove(handler_id)
    return list(caplog.records)


def test_dangling_secret_logs_an_error_naming_the_fix(caplog, monkeypatch):
    m = _manager_raising(SecretNotFoundError("GONE"), monkeypatch)
    records = _capture(caplog, m._init_channels)

    errors = [r for r in records if r.levelname == "ERROR"]
    assert errors, "a dangling secret reference must not be a mere warning"
    msg = errors[0].getMessage()
    assert "websocket" in msg
    assert "GONE" in msg
    assert "durin secret set" in msg          # actionable
    assert "other channels keep running" in msg  # scope of the damage


def test_other_startup_failures_stay_warnings(caplog, monkeypatch):
    """Only the configuration error is escalated; transient causes are not."""
    m = _manager_raising(RuntimeError("network down"), monkeypatch)
    records = _capture(caplog, m._init_channels)

    assert [r.levelname for r in records] == ["WARNING"]
    assert "not available" in records[0].getMessage()


def test_the_gateway_survives_the_failed_channel(monkeypatch):
    m = _manager_raising(SecretNotFoundError("GONE"), monkeypatch)
    m._init_channels()
    assert m.channels == {}   # the channel is skipped, nothing raises out
