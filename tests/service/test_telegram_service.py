"""Unit tests for TelegramService.test (getMe token-test, persists nothing)."""

from __future__ import annotations

import pytest

from durin.pairing import store
from durin.service.channels_telegram import (
    PairingApproveCommand,
    PairingDenyCommand,
    PairingListQuery,
    PairingRevokeCommand,
    TelegramService,
    TelegramTestCommand,
)
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


async def test_token_test_persists_nothing(monkeypatch):
    """test() must never write to the secret store or the config file."""

    def _forbidden_store_secret(*a, **kw):
        raise AssertionError("store_secret must not be called by the test endpoint")

    def _forbidden_save_config(*a, **kw):
        raise AssertionError("save_config must not be called by the test endpoint")

    monkeypatch.setattr("durin.security.secrets.store_secret", _forbidden_store_secret)
    monkeypatch.setattr("durin.config.loader.save_config", _forbidden_save_config)

    class FakeBot:
        def __init__(self, token): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get_me(self):
            class U: id = 7; username = "safe_bot"
            return U()

    monkeypatch.setattr("durin.service.channels_telegram.Bot", FakeBot)
    res = await TelegramService().test(TelegramTestCommand(token="123:abc"), Principal.local())
    assert res.ok is True


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


# ---------------------------------------------------------------------------
# Pairing endpoints
# ---------------------------------------------------------------------------


async def test_pairing_list_and_approve():
    code = store.generate_code("telegram", "99999")
    svc = TelegramService()
    listed = await svc.pairing(PairingListQuery(), Principal.local())
    assert any(p["code"] == code for p in listed.pending)
    res = await svc.pairing_approve(PairingApproveCommand(code=code), Principal.local())
    assert res.ok is True
    approved = await svc.pairing(PairingListQuery(), Principal.local())
    assert "99999" in approved.approved


async def test_pairing_list_filters_to_telegram():
    """list_pending may include entries from other channels — only telegram's are returned."""
    store.generate_code("slack", "slack-user-1")
    code = store.generate_code("telegram", "tg-user-2")
    svc = TelegramService()
    listed = await svc.pairing(PairingListQuery(), Principal.local())
    assert all(p["channel"] == "telegram" for p in listed.pending)
    assert any(p["code"] == code for p in listed.pending)


async def test_pairing_deny():
    code = store.generate_code("telegram", "44444")
    svc = TelegramService()
    res = await svc.pairing_deny(PairingDenyCommand(code=code), Principal.local())
    assert res.ok is True
    listed = await svc.pairing(PairingListQuery(), Principal.local())
    assert not any(p["code"] == code for p in listed.pending)


async def test_pairing_deny_unknown_code():
    svc = TelegramService()
    res = await svc.pairing_deny(PairingDenyCommand(code="XXXX-XXXX"), Principal.local())
    assert res.ok is False


async def test_pairing_revoke():
    code = store.generate_code("telegram", "55555")
    store.approve_code(code)
    svc = TelegramService()
    approved = await svc.pairing(PairingListQuery(), Principal.local())
    assert "55555" in approved.approved
    res = await svc.pairing_revoke(PairingRevokeCommand(sender_id="55555"), Principal.local())
    assert res.ok is True
    after = await svc.pairing(PairingListQuery(), Principal.local())
    assert "55555" not in after.approved


async def test_pairing_revoke_unknown_sender():
    svc = TelegramService()
    res = await svc.pairing_revoke(PairingRevokeCommand(sender_id="no-such-user"), Principal.local())
    assert res.ok is False


async def test_pairing_approve_unknown_code():
    svc = TelegramService()
    res = await svc.pairing_approve(PairingApproveCommand(code="ZZZZ-ZZZZ"), Principal.local())
    assert res.ok is False
    assert res.channel is None
    assert res.sender_id is None


def test_gateway_registry_registers_telegram_like_the_catalog():
    """The gateway builds its registry via wiring.build_service_registry, NOT
    catalog.build_catalog_registry. A service registered in one but not the
    other is served only by tests/standalone-api and silently 405s on the live
    gateway. Freeze that the two register the same service set."""
    from durin.service.catalog import build_catalog_registry
    from durin.service.wiring import build_service_registry

    wiring = build_service_registry(
        config=None, session_manager=None, cron_service=None, bus=None
    )
    wnames = {b.service_name for b in wiring.routes}
    cnames = {b.service_name for b in build_catalog_registry().routes}
    assert wnames == cnames, (
        f"registry drift — catalog-only={cnames - wnames}, "
        f"wiring-only={wnames - cnames}"
    )
    assert "telegram" in wnames
    assert any(b.spec.path.endswith("/telegram/test") for b in wiring.routes)
