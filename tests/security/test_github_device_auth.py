"""Device-flow parse/state-machine + shared-secret round-trip."""

import pytest

from durin.security import github_device_auth as gda
from durin.security.github_auth import resolve_github_token


def test_start_parses_and_sends_client_id_and_scope():
    seen = {}

    def poster(url, data):
        seen.update(url=url, data=data)
        return {
            "device_code": "dc",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "verification_uri_complete": "https://github.com/login/device?user_code=WDJB-MJHT",
            "interval": 5,
            "expires_in": 900,
        }

    dc = gda.start_device_flow(scope="read:user", poster=poster)
    assert dc.user_code == "WDJB-MJHT"
    assert dc.verification_uri_complete.endswith("WDJB-MJHT")
    assert dc.interval == 5
    assert seen["url"] == gda.DEVICE_CODE_URL
    assert seen["data"]["client_id"] == gda.CLIENT_ID
    assert seen["data"]["scope"] == "read:user"


def test_start_completes_uri_falls_back_to_plain():
    dc = gda.start_device_flow(
        poster=lambda u, d: {
            "device_code": "dc",
            "user_code": "X",
            "verification_uri": "https://gh/dev",
            "interval": 5,
            "expires_in": 900,
        }
    )
    assert dc.verification_uri_complete == "https://gh/dev"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"access_token": "gho_x", "scope": "read:user"}, "authorized"),
        ({"error": "authorization_pending"}, "pending"),
        ({"error": "slow_down"}, "slow_down"),
        ({"error": "expired_token"}, "expired"),
        ({"error": "access_denied"}, "denied"),
        ({"error": "weird"}, "error"),
        ({}, "error"),
    ],
)
def test_exchange_state_machine(payload, expected):
    res = gda.exchange_device_code("dc", poster=lambda u, d: payload)
    assert res.status == expected
    if expected == "authorized":
        assert res.access_token == "gho_x"
        assert res.scope == "read:user"


def test_exchange_sends_device_grant():
    seen = {}

    def poster(url, data):
        seen.update(url=url, data=data)
        return {"error": "authorization_pending"}

    gda.exchange_device_code("DEV123", poster=poster)
    assert seen["url"] == gda.ACCESS_TOKEN_URL
    assert seen["data"]["device_code"] == "DEV123"
    assert seen["data"]["grant_type"] == gda.DEVICE_GRANT


def test_store_then_resolve_then_forget_roundtrip():
    # DURIN_HOME is isolated per-test (conftest autouse) -> hits a tmp store.
    assert resolve_github_token(env={}, gh_runner=lambda: None) == ""
    gda.store_github_token("gho_TESTTOKEN")
    assert resolve_github_token(env={}, gh_runner=lambda: None) == "gho_TESTTOKEN"
    assert gda.forget_github_token() is True
    assert resolve_github_token(env={}, gh_runner=lambda: None) == ""
    # forgetting again is a no-op
    assert gda.forget_github_token() is False
