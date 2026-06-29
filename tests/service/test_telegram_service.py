"""Unit tests for TelegramService.test (getMe token-test, persists nothing)."""

from __future__ import annotations

import pytest

from durin.service.channels_telegram import TelegramService, TelegramTestCommand
from durin.service.principal import Principal


async def test_token_test_ok(monkeypatch):
    class FakeBot:
        def __init__(self, token): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_me(self):
            class U: id = 42; username = "durin_bot"
            return U()

    monkeypatch.setattr("durin.service.channels_telegram.Bot", FakeBot)
    res = await TelegramService().test(TelegramTestCommand(token="123:abc"), Principal.local())
    assert res.ok is True and res.username == "durin_bot" and res.id == 42


async def test_token_test_bad(monkeypatch):
    class FakeBot:
        def __init__(self, token): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_me(self):
            raise RuntimeError("Unauthorized")

    monkeypatch.setattr("durin.service.channels_telegram.Bot", FakeBot)
    res = await TelegramService().test(TelegramTestCommand(token="bad"), Principal.local())
    assert res.ok is False and res.error == "RuntimeError"


async def test_token_test_error_does_not_expose_token(monkeypatch):
    """Error field must never contain the token string."""
    class FakeBot:
        def __init__(self, token): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_me(self):
            raise ValueError("invalid token abc:secret")

    monkeypatch.setattr("durin.service.channels_telegram.Bot", FakeBot)
    res = await TelegramService().test(TelegramTestCommand(token="abc:secret"), Principal.local())
    assert res.ok is False
    assert "abc:secret" not in (res.error or "")
    assert res.error == "ValueError"
