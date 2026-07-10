from __future__ import annotations

import pytest

pytest.importorskip("discord")

from durin.pairing import store
from durin.service.channels_discord import (
    DiscordGuildsListQuery,
    DiscordInviteQuery,
    DiscordService,
    DiscordTestCommand,
    PairingApproveCommand,
    PairingDenyCommand,
    PairingListQuery,
    PairingRevokeCommand,
)
from durin.service.principal import Principal

TOKEN = "MTUyNDg2MzU3MDA5MTk2NjU0NA.GkxhNX.SUPERSECRETVALUE"


class _FakeUser:
    id = 1524863570091966544

    def __str__(self) -> str:  # discord.py renders name#disc, or just name
        return "durin"


class _FakeFlags:
    def __init__(self, enabled=False, limited=False):
        self.gateway_message_content = enabled
        self.gateway_message_content_limited = limited


class _FakeApp:
    def __init__(self, flags):
        self.id = 1524863570091966544
        self.flags = flags


class _FakeClient:
    """Stands in for a REST-only, logged-in discord.py client."""

    def __init__(self, *, flags=None, guilds=None, raise_on_login=None):
        self.user = _FakeUser()
        self._flags = flags or _FakeFlags(enabled=True)
        self._guilds = guilds if guilds is not None else [{"id": "1", "name": "s"}]
        self._raise = raise_on_login
        self.closed = False

    async def login(self, token):
        if self._raise:
            raise self._raise

    async def close(self):
        self.closed = True

    async def application_info(self):
        return _FakeApp(self._flags)

    async def fetch_guilds_rest(self):
        return self._guilds


@pytest.fixture(autouse=True)
def _isolated_pairing(tmp_path, monkeypatch):
    """DURIN_HOME is the only isolation the pairing store honours: it derives
    its path per call. A `_STORE_PATH` pin looks reassuring and does nothing —
    the attribute does not exist, so tests would write the real store."""
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    yield


# --------------------------------------------------------------------- test


@pytest.mark.asyncio
async def test_test_reports_identity_and_read_permission(monkeypatch):
    svc = DiscordService()
    monkeypatch.setattr(
        "durin.service.channels_discord._rest_client",
        lambda: _FakeClient(flags=_FakeFlags(enabled=True)),
    )
    res = await svc.test(DiscordTestCommand(token=TOKEN), Principal.local())
    assert res.ok is True
    assert res.bot_user == "durin"
    assert res.application_id == "1524863570091966544"
    assert res.message_content_intent == "enabled"


@pytest.mark.asyncio
async def test_test_reports_limited_intent_distinctly(monkeypatch):
    """An unverified app under 100 guilds reads as 'limited', not 'enabled':
    Discord revokes it at the threshold, so it must be surfaced."""
    svc = DiscordService()
    monkeypatch.setattr(
        "durin.service.channels_discord._rest_client",
        lambda: _FakeClient(flags=_FakeFlags(limited=True)),
    )
    res = await svc.test(DiscordTestCommand(token=TOKEN), Principal.local())
    assert res.message_content_intent == "limited"


@pytest.mark.asyncio
async def test_test_reports_disabled_intent(monkeypatch):
    svc = DiscordService()
    monkeypatch.setattr(
        "durin.service.channels_discord._rest_client",
        lambda: _FakeClient(flags=_FakeFlags()),
    )
    res = await svc.test(DiscordTestCommand(token=TOKEN), Principal.local())
    assert res.ok is True
    assert res.message_content_intent == "disabled"


@pytest.mark.asyncio
async def test_test_falls_back_to_the_configured_token(monkeypatch):
    """The connected panel re-verifies a stored token without re-pasting it."""
    seen: list[str] = []

    def _client():
        return _FakeClient()

    monkeypatch.setattr("durin.service.channels_discord._rest_client", _client)
    monkeypatch.setattr(
        "durin.service.channels_discord._configured_token", lambda: "CONFIGURED-TOKEN"
    )
    async def _capture_login(client, token):
        seen.append(token)

    monkeypatch.setattr("durin.service.channels_discord._login", _capture_login)
    svc = DiscordService()
    res = await svc.test(DiscordTestCommand(), Principal.local())
    assert res.ok is True
    assert seen == ["CONFIGURED-TOKEN"]


@pytest.mark.asyncio
async def test_test_without_any_token_is_not_configured(monkeypatch):
    monkeypatch.setattr("durin.service.channels_discord._configured_token", lambda: None)
    svc = DiscordService()
    res = await svc.test(DiscordTestCommand(), Principal.local())
    assert res.ok is False
    assert res.error == "not_configured"


@pytest.mark.asyncio
async def test_test_maps_bad_token_to_a_curated_code(monkeypatch):
    import discord

    monkeypatch.setattr(
        "durin.service.channels_discord._rest_client",
        lambda: _FakeClient(raise_on_login=discord.LoginFailure("bad")),
    )
    svc = DiscordService()
    res = await svc.test(DiscordTestCommand(token=TOKEN), Principal.local())
    assert res.ok is False
    assert res.error == "invalid_token"


@pytest.mark.asyncio
async def test_test_never_echoes_the_token(monkeypatch):
    monkeypatch.setattr(
        "durin.service.channels_discord._rest_client",
        lambda: _FakeClient(raise_on_login=RuntimeError(f"boom {TOKEN}")),
    )
    svc = DiscordService()
    res = await svc.test(DiscordTestCommand(token=TOKEN), Principal.local())
    assert res.ok is False
    assert TOKEN not in (res.error or "")
    assert "SUPERSECRET" not in (res.error or "")


@pytest.mark.asyncio
async def test_test_persists_nothing(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("test must not persist")

    monkeypatch.setattr("durin.security.secrets.store_secret", _boom)
    monkeypatch.setattr("durin.config.loader.save_config", _boom)
    monkeypatch.setattr(
        "durin.service.channels_discord._rest_client", lambda: _FakeClient()
    )
    svc = DiscordService()
    await svc.test(DiscordTestCommand(token=TOKEN), Principal.local())


# ------------------------------------------------------------------ pairing


@pytest.mark.asyncio
async def test_pairing_list_filters_to_discord(monkeypatch):
    store.generate_code("discord", "111")
    store.generate_code("telegram", "222")
    monkeypatch.setattr("durin.service.channels_discord._display_names", _no_names)
    svc = DiscordService()
    res = await svc.pairing(PairingListQuery(), Principal.local())
    assert [p["sender_id"] for p in res.pending] == ["111"]


@pytest.mark.asyncio
async def test_pairing_list_resolves_display_names(monkeypatch):
    store.generate_code("discord", "111")

    async def _names(ids):
        return {"111": "Marcelo Marmol"}

    monkeypatch.setattr("durin.service.channels_discord._display_names", _names)
    svc = DiscordService()
    res = await svc.pairing(PairingListQuery(), Principal.local())
    assert res.names == {"111": "Marcelo Marmol"}


async def _no_names(ids):
    return {}


@pytest.mark.asyncio
async def test_pairing_approve_deny_revoke(monkeypatch):
    monkeypatch.setattr("durin.service.channels_discord._display_names", _no_names)
    svc = DiscordService()

    code = store.generate_code("discord", "111")
    res = await svc.pairing_approve(PairingApproveCommand(code=code), Principal.local())
    assert res.ok and res.channel == "discord" and res.sender_id == "111"
    assert store.is_approved("discord", "111")

    rev = await svc.pairing_revoke(PairingRevokeCommand(sender_id="111"), Principal.local())
    assert rev.ok and not store.is_approved("discord", "111")

    code2 = store.generate_code("discord", "222")
    den = await svc.pairing_deny(PairingDenyCommand(code=code2), Principal.local())
    assert den.ok
    assert not any(p["code"] == code2 for p in store.list_pending())


# ------------------------------------------------------------------- guilds


@pytest.mark.asyncio
async def test_guilds_lists_only_messageable_channels(monkeypatch):
    """Voice, category and stage channels can never receive a message, so they
    must not appear as allowlist checkboxes."""
    guilds = [{"id": "10", "name": "Server"}]
    channels = [
        {"id": "1", "name": "general", "type": 0},   # text
        {"id": "2", "name": "soporte", "type": 15},  # forum
        {"id": "3", "name": "avisos", "type": 5},    # announcement
        {"id": "4", "name": "Voz", "type": 2},       # voice -> dropped
        {"id": "5", "name": "Cat", "type": 4},       # category -> dropped
    ]
    monkeypatch.setattr("durin.service.channels_discord._configured_token", lambda: TOKEN)
    monkeypatch.setattr(
        "durin.service.channels_discord._rest_guild_tree", _fake_tree(guilds, channels)
    )
    svc = DiscordService()
    res = await svc.guilds(DiscordGuildsListQuery(), Principal.local())
    assert res.ok
    names = [c["name"] for c in res.guilds[0]["channels"]]
    assert names == ["general", "soporte", "avisos"]


@pytest.mark.asyncio
async def test_guilds_marks_allowed_channels(monkeypatch):
    monkeypatch.setattr("durin.service.channels_discord._configured_token", lambda: TOKEN)
    monkeypatch.setattr(
        "durin.service.channels_discord._rest_guild_tree",
        _fake_tree([{"id": "10", "name": "S"}], [{"id": "1", "name": "general", "type": 0}]),
    )
    monkeypatch.setattr(
        "durin.service.channels_discord._configured_allow_channels", lambda: ["1"]
    )
    svc = DiscordService()
    res = await svc.guilds(DiscordGuildsListQuery(), Principal.local())
    assert res.guilds[0]["channels"][0]["allowed"] is True


@pytest.mark.asyncio
async def test_guilds_without_token_is_not_configured(monkeypatch):
    monkeypatch.setattr("durin.service.channels_discord._configured_token", lambda: None)
    svc = DiscordService()
    res = await svc.guilds(DiscordGuildsListQuery(), Principal.local())
    assert res.ok is False and res.error == "not_configured"


def _fake_tree(guilds, channels):
    async def _f(token):
        return [(g, channels) for g in guilds]

    return _f


# ------------------------------------------------------------------- invite


@pytest.mark.asyncio
async def test_invite_url_is_least_privilege(monkeypatch):
    """No Administrator. Exactly the permissions durin exercises."""
    monkeypatch.setattr("durin.service.channels_discord._configured_token", lambda: TOKEN)
    svc = DiscordService()
    res = await svc.invite(DiscordInviteQuery(), Principal.local())
    assert res.ok
    assert res.permissions == "274878024768"
    assert "permissions=274878024768" in res.url
    assert "client_id=1524863570091966544" in res.url
    assert "scope=bot+applications.commands" in res.url
    assert "permissions=8" not in res.url


@pytest.mark.asyncio
async def test_invite_without_token_is_not_configured(monkeypatch):
    monkeypatch.setattr("durin.service.channels_discord._configured_token", lambda: None)
    svc = DiscordService()
    res = await svc.invite(DiscordInviteQuery(), Principal.local())
    assert res.ok is False and res.error == "not_configured"


def test_application_id_decodes_from_token():
    from durin.service.channels_discord import _application_id_from_token

    assert _application_id_from_token(TOKEN) == "1524863570091966544"
    assert _application_id_from_token("garbage") is None


# ---------------------------------------------------------------- registries


def test_gateway_registry_registers_discord_like_the_catalog():
    """A service registered in one registry but not the other is served only by
    tests and silently 405s on the live gateway."""
    from durin.service.catalog import build_catalog_registry
    from durin.service.wiring import build_service_registry

    wiring = build_service_registry(
        config=None, session_manager=None, cron_service=None, bus=None
    )
    wnames = {b.service_name for b in wiring.routes}
    cnames = {b.service_name for b in build_catalog_registry().routes}
    assert wnames == cnames, (
        f"registry drift — catalog-only={cnames - wnames}, wiring-only={wnames - cnames}"
    )
    assert "discord" in wnames
    for suffix in ("/discord/test", "/discord/pairing", "/discord/guilds", "/discord/invite"):
        assert any(b.spec.path.endswith(suffix) for b in wiring.routes), suffix


@pytest.mark.asyncio
async def test_test_returns_the_invite_url_so_the_ui_never_duplicates_permissions(monkeypatch):
    """The permission bitfield is a security constant. If the frontend rebuilt
    the invite URL it would hold a second copy, free to drift."""
    monkeypatch.setattr(
        "durin.service.channels_discord._rest_client", lambda: _FakeClient()
    )
    svc = DiscordService()
    res = await svc.test(DiscordTestCommand(token=TOKEN), Principal.local())
    assert res.invite_url is not None
    assert "permissions=274878024768" in res.invite_url
    assert "client_id=1524863570091966544" in res.invite_url
    assert "permissions=8" not in res.invite_url
