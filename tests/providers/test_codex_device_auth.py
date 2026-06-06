import base64
import json
import time

import pytest

pytest.importorskip("oauth_cli_kit")

from durin.providers import codex_device_auth as cda


def _make_jwt(claims: dict) -> str:
    def seg(d: dict) -> str:
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def test_account_id_from_jwt_reads_nested_claim():
    tok = _make_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}})
    assert cda.account_id_from_jwt(tok) == "acct_123"


def test_account_id_from_jwt_tolerates_garbage():
    assert cda.account_id_from_jwt("not-a-jwt") is None
    assert cda.account_id_from_jwt("") is None


def test_expiry_ms_from_jwt_uses_exp_claim():
    exp = int(time.time()) + 3600
    tok = _make_jwt({"exp": exp})
    assert cda.expiry_ms_from_jwt(tok) == exp * 1000


def test_expiry_ms_from_jwt_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(cda.time, "time", lambda: 1000.0)
    tok = _make_jwt({"no": "exp"})
    assert cda.expiry_ms_from_jwt(tok) == (1000 + 3600) * 1000


import httpx  # noqa: E402

from oauth_cli_kit.models import OAuthToken  # noqa: E402


def _mock_client(handler):
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)

    return factory


def test_request_device_code_parses_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/deviceauth/usercode")
        assert json.loads(request.content)["client_id"] == cda.CLIENT_ID
        return httpx.Response(
            200,
            json={"user_code": "WXYZ-1234", "device_auth_id": "dev_1", "interval": 5},
        )

    monkeypatch.setattr(cda, "_client", _mock_client(handler))
    ch = cda.request_device_code()
    assert ch.user_code == "WXYZ-1234"
    assert ch.device_auth_id == "dev_1"
    assert ch.verification_uri == cda.VERIFICATION_URI


def test_poll_once_pending_on_403(monkeypatch):
    monkeypatch.setattr(
        cda, "_client", _mock_client(lambda req: httpx.Response(403, json={}))
    )
    res = cda.poll_once("dev_1", "WXYZ-1234")
    assert res.status == "pending" and res.token is None


def test_poll_once_ok_exchanges_and_persists(monkeypatch, tmp_path):
    exp = int(time.time()) + 3600
    access = _make_jwt(
        {
            "exp": exp,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct_9"},
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deviceauth/token"):
            return httpx.Response(
                200, json={"authorization_code": "AC", "code_verifier": "CV"}
            )
        if request.url.path.endswith("/oauth/token"):
            assert b"grant_type=authorization_code" in request.content
            return httpx.Response(200, json={"access_token": access, "refresh_token": "RT"})
        raise AssertionError(request.url.path)

    saved: list[OAuthToken] = []

    class _Storage:
        def save(self, tok):
            saved.append(tok)

        def load(self):
            return saved[-1] if saved else None

        def get_token_path(self):
            return tmp_path / "codex.json"

    monkeypatch.setattr(cda, "_client", _mock_client(handler))
    monkeypatch.setattr(cda, "_strict_storage", lambda: _Storage())
    res = cda.poll_once("dev_1", "WXYZ-1234")
    assert res.status == "ok"
    assert res.token.account_id == "acct_9"
    assert res.token.access == access
    assert res.token.refresh == "RT"
    assert res.token.expires == exp * 1000
    assert saved and saved[-1].account_id == "acct_9"
