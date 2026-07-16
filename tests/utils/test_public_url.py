"""Resolution of durin's public base URL (gateway.public_url + browser origin)."""
from __future__ import annotations

from loguru import logger

from durin.config.schema import Config
from durin.utils.public_url import dashboard_url, resolve_public_base_url, validate_origin


def _cfg(public_url=None, ws_host=None, ws_port=None) -> Config:
    cfg = Config()
    if public_url is not None:
        cfg.gateway.public_url = public_url
    if ws_host or ws_port:
        extra = getattr(cfg.channels, "__pydantic_extra__", None)
        ws = {"enabled": True}
        if ws_host:
            ws["host"] = ws_host
        if ws_port:
            ws["port"] = ws_port
        if isinstance(extra, dict):
            extra["websocket"] = ws
    return cfg


def test_resolve_returns_none_when_unset():
    assert resolve_public_base_url(_cfg()) is None


def test_resolve_normalizes_trailing_slash():
    assert (
        resolve_public_base_url(_cfg("https://durin.tail9e5f5d.ts.net/"))
        == "https://durin.tail9e5f5d.ts.net"
    )


def test_resolve_rejects_url_with_path():
    assert resolve_public_base_url(_cfg("https://x.example/app")) is None


def test_resolve_warns_once_when_set_but_invalid():
    """An operator typo in gateway.public_url must not fail silently — it
    warns once (naming the bad value) each time resolution is attempted."""
    warnings: list[str] = []
    handler_id = logger.add(lambda m: warnings.append(str(m)), level="WARNING", format="{message}")
    try:
        result = resolve_public_base_url(_cfg("not a url"))
    finally:
        logger.remove(handler_id)
    assert result is None
    assert len(warnings) == 1
    assert "not a url" in warnings[0]


def test_resolve_no_warning_when_unset():
    warnings: list[str] = []
    handler_id = logger.add(lambda m: warnings.append(str(m)), level="WARNING", format="{message}")
    try:
        result = resolve_public_base_url(_cfg())
    finally:
        logger.remove(handler_id)
    assert result is None
    assert warnings == []


def test_validate_origin_accepts_bare_http_origin():
    assert validate_origin("http://durin:8765") == "http://durin:8765"
    assert validate_origin("https://durin.tail9e5f5d.ts.net") == "https://durin.tail9e5f5d.ts.net"


def test_validate_origin_rejects_junk():
    assert validate_origin("") is None
    assert validate_origin("ftp://x") is None
    assert validate_origin("http://durin:8765/path") is None
    assert validate_origin("http://durin:8765?q=1") is None
    assert validate_origin("not a url") is None


def test_dashboard_url_prefers_public_url():
    assert dashboard_url(_cfg("https://durin.tail9e5f5d.ts.net")) == "https://durin.tail9e5f5d.ts.net"


def test_dashboard_url_falls_back_to_ws_host_port():
    assert dashboard_url(_cfg(ws_host="100.1.2.3", ws_port=8765)) == "http://100.1.2.3:8765"
