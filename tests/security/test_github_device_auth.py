"""Device-flow parse/state-machine + shared-secret round-trip."""

import httpx
import pytest

from durin.security import github_device_auth as gda
from durin.security.github_auth import resolve_github_token


def _http_status_error(status: int, json_body=None, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", gda.ACCESS_TOKEN_URL)
    response = (
        httpx.Response(status, json=json_body, request=request)
        if json_body is not None
        else httpx.Response(status, text=text, request=request)
    )
    return httpx.HTTPStatusError("boom", request=request, response=response)


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


@pytest.mark.parametrize(
    "raiser",
    [
        lambda u, d: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        lambda u, d: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        lambda u, d: (_ for _ in ()).throw(_http_status_error(503, text="unavailable")),
        lambda u, d: (_ for _ in ()).throw(_http_status_error(429, text="rate limited")),
        lambda u, d: (_ for _ in ()).throw(_http_status_error(400, text="<html>not json</html>")),
    ],
)
def test_exchange_transient_failures_do_not_raise(raiser):
    res = gda.exchange_device_code("dc", poster=raiser)
    assert res.status == "transient"
    assert res.error


def test_exchange_maps_rfc8628_style_400_error_body():
    # An OAuth error delivered as HTTP 400 + JSON must hit the state machine,
    # not read as a retryable hiccup: a denied flow has to end.
    def poster(u, d):
        raise _http_status_error(400, json_body={"error": "access_denied"})

    res = gda.exchange_device_code("dc", poster=poster)
    assert res.status == "denied"


def test_poll_flow_survives_transient_failure_then_authorizes():
    ch = gda.request_device_code(
        poster=lambda u, d: {
            "device_code": "DC",
            "user_code": "X",
            "verification_uri": "u",
            "interval": 5,
            "expires_in": 900,
        },
        now=lambda: 1000.0,
    )

    def failing_poster(u, d):
        raise httpx.ConnectError("refused")

    r1 = gda.poll_flow(ch.flow_id, poster=failing_poster, now=lambda: 1001.0)
    assert r1.status == "transient"

    # the flow must still be alive after the hiccup
    r2 = gda.poll_flow(
        ch.flow_id,
        poster=lambda u, d: {"access_token": "gho_OK2", "scope": "read:user"},
        now=lambda: 1002.0,
    )
    assert r2.status == "authorized"
    assert resolve_github_token(env={}, gh_runner=lambda: None) == "gho_OK2"
    # clean up: the secret-store cache outlives this test's DURIN_HOME
    assert gda.forget_github_token() is True


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


def test_request_device_code_hides_raw_device_code():
    payload = {
        "device_code": "SECRETDC",
        "user_code": "AB-CD",
        "verification_uri": "https://gh/dev",
        "verification_uri_complete": "https://gh/dev?c=AB-CD",
        "interval": 5,
        "expires_in": 900,
    }
    ch = gda.request_device_code(poster=lambda u, d: payload, now=lambda: 1000.0)
    assert ch.user_code == "AB-CD"
    assert ch.flow_id
    # the poll secret must never travel to the browser on the challenge
    assert "SECRETDC" not in (ch.flow_id + ch.user_code + ch.verification_uri_complete)


def test_poll_flow_unknown_is_expired():
    assert gda.poll_flow("nope", poster=lambda u, d: {}).status == "expired"


def test_poll_flow_pending_then_authorized_stores_and_consumes():
    ch = gda.request_device_code(
        poster=lambda u, d: {
            "device_code": "DC",
            "user_code": "X",
            "verification_uri": "u",
            "interval": 5,
            "expires_in": 900,
        },
        now=lambda: 1000.0,
    )
    r1 = gda.poll_flow(
        ch.flow_id, poster=lambda u, d: {"error": "authorization_pending"}, now=lambda: 1001.0
    )
    assert r1.status == "pending"
    assert resolve_github_token(env={}, gh_runner=lambda: None) == ""

    r2 = gda.poll_flow(
        ch.flow_id,
        poster=lambda u, d: {"access_token": "gho_OK", "scope": "read:user"},
        now=lambda: 1002.0,
    )
    assert r2.status == "authorized"
    assert resolve_github_token(env={}, gh_runner=lambda: None) == "gho_OK"

    # flow consumed after success
    assert gda.poll_flow(ch.flow_id, poster=lambda u, d: {}, now=lambda: 1003.0).status == "expired"


def test_poll_flow_past_deadline_is_expired():
    ch = gda.request_device_code(
        poster=lambda u, d: {
            "device_code": "DC",
            "user_code": "X",
            "verification_uri": "u",
            "interval": 5,
            "expires_in": 10,
        },
        now=lambda: 1000.0,
    )
    res = gda.poll_flow(ch.flow_id, poster=lambda u, d: {"access_token": "x"}, now=lambda: 2000.0)
    assert res.status == "expired"


def test_github_status_no_token():
    st = gda.github_status(resolver=lambda: ("", ""), get=lambda u, t: (200, {}, {}))
    assert st.connected is False
    assert st.reachable is False
    assert st.source == ""


def test_github_status_reachable_reports_source_login_scopes_rate():
    def get(url, token):
        assert token == "gho_X"
        return (
            200,
            {"login": "marcelo"},
            {
                "X-OAuth-Scopes": "read:user, repo",
                "X-RateLimit-Remaining": "4982",
                "X-RateLimit-Limit": "5000",
            },
        )

    st = gda.github_status(resolver=lambda: ("gho_X", "secret"), get=get)
    assert st.connected and st.reachable
    assert st.source == "secret"
    assert st.login == "marcelo"
    assert st.scopes == "read:user, repo"
    assert st.rate_remaining == 4982
    assert st.rate_limit == 5000


def test_github_status_carries_source_even_when_unreachable():
    st = gda.github_status(
        resolver=lambda: ("gho_gh", "gh"), get=lambda u, t: (401, {"message": "Bad"}, {})
    )
    assert st.connected is True
    assert st.reachable is False
    assert st.source == "gh"  # so the UI can say "via gh CLI" even if the token is stale
