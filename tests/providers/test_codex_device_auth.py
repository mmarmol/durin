import base64
import json
import time
import types

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


def test_existing_codex_session_reads_durin_token(monkeypatch):
    access = _make_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct_9",
                "chatgpt_plan_type": "pro",
            },
            "https://api.openai.com/profile.email": "u@x.com",
        }
    )

    class _Storage:
        def load(self):
            return OAuthToken(access=access, refresh="RT", expires=10**13, account_id="acct_9")

    monkeypatch.setattr(cda, "_strict_storage", lambda: _Storage())
    monkeypatch.setattr(cda, "_read_codex_cli_session", lambda: None)
    info = cda.existing_codex_session()
    assert info is not None
    assert info.email == "u@x.com"
    assert info.plan == "pro"
    assert info.source == "durin"


def test_existing_codex_session_none_when_absent(monkeypatch):
    class _Storage:
        def load(self):
            return None

    monkeypatch.setattr(cda, "_strict_storage", lambda: _Storage())
    monkeypatch.setattr(cda, "_read_codex_cli_session", lambda: None)
    assert cda.existing_codex_session() is None


def test_disconnect_removes_secret_and_legacy_file(monkeypatch, tmp_path):
    import durin.security.secrets as secmod

    legacy = tmp_path / "codex.json"
    legacy.write_text("{}")
    lock_path = tmp_path / "codex.lock"  # legacy.with_suffix(".lock")
    lock_path.write_text("")
    monkeypatch.setattr(
        cda,
        "_kit_file_storage",
        lambda: types.SimpleNamespace(get_token_path=lambda: legacy),
    )

    removed_names: list[str] = []

    class _FakeStore:
        def load(self):
            return self

        def remove(self, name):
            removed_names.append(name)
            return True

        def save(self):
            return None

    monkeypatch.setattr(secmod, "SecretStore", _FakeStore)
    monkeypatch.setattr(secmod, "get_secret_store", lambda **k: None)

    assert cda.disconnect() is True
    assert "OPENAI_CODEX_OAUTH" in removed_names
    assert not legacy.exists()
    assert not lock_path.exists()


import socket as _socket


def _free_1455():
    # Skip if something already holds :1455 (e.g. a real gateway loopback attempt).
    for fam, host in ((_socket.AF_INET, "127.0.0.1"), (_socket.AF_INET6, "::1")):
        s = _socket.socket(fam)
        try:
            if s.connect_ex((host, 1455)) == 0:
                return False
        finally:
            s.close()
    return True


def test_build_authorize_url_params():
    url = cda._build_authorize_url("CHAL", "STATE")
    assert url.startswith("https://auth.openai.com/oauth/authorize?")
    assert "originator=codex_cli_rs" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback" in url
    assert "state=STATE" in url


def test_callback_server_binds_ipv4():
    if not _free_1455():
        pytest.skip(":1455 already in use")
    result = cda._CallbackResult()
    servers = cda._start_callback_servers("st", result)
    try:
        assert servers, "no callback server bound"
        s = _socket.socket(_socket.AF_INET)
        s.settimeout(1.0)
        # The whole point of the fix: the browser hits localhost -> 127.0.0.1.
        assert s.connect_ex(("127.0.0.1", 1455)) == 0
        s.close()
    finally:
        for srv in servers:
            srv.shutdown()


def test_start_loopback_returns_url_and_listens(monkeypatch):
    if not _free_1455():
        pytest.skip(":1455 already in use")
    cda._loopback_state["thread"] = None
    cda._loopback_state["url"] = None
    url = cda.start_loopback_login(max_wait_s=0.2)
    try:
        assert "code_challenge=" in url and "originator=codex_cli_rs" in url
        s = _socket.socket(_socket.AF_INET)
        s.settimeout(1.0)
        assert s.connect_ex(("127.0.0.1", 1455)) == 0  # listening before we returned
        s.close()
    finally:
        # let the background thread time out (0.2s) and shut the servers down
        time.sleep(0.4)


def test_codex_secrets_storage_roundtrip(monkeypatch):
    from oauth_cli_kit.models import OAuthToken

    import durin.security.secrets as secmod

    store: dict[str, str] = {}

    def fake_store(name, value, **kw):
        ref = f"${{secret:{name}}}"
        store[ref] = value
        return ref

    def fake_resolve(ref):
        if isinstance(ref, str) and ref in store:
            return store[ref]
        raise secmod.SecretNotFoundError(str(ref))

    monkeypatch.setattr(secmod, "store_secret", fake_store)
    monkeypatch.setattr(secmod, "resolve_secret", fake_resolve)
    monkeypatch.setattr(cda, "_kit_file_storage", lambda: types.SimpleNamespace(load=lambda: None))

    s = cda._CodexSecretsStorage()
    assert s.load() is None
    s.save(OAuthToken(access="A", refresh="R", expires=123, account_id="acct"))
    loaded = s.load()
    assert loaded.access == "A"
    assert loaded.refresh == "R"
    assert loaded.expires == 123
    assert loaded.account_id == "acct"
    assert cda.codex_token_present() is True


def test_codex_secrets_storage_migrates_from_kit_file(monkeypatch):
    from oauth_cli_kit.models import OAuthToken

    import durin.security.secrets as secmod

    store: dict[str, str] = {}
    saved: list[str] = []

    def fake_store(name, value, **kw):
        ref = f"${{secret:{name}}}"
        store[ref] = value
        saved.append(value)
        return ref

    def fake_resolve(ref):
        if isinstance(ref, str) and ref in store:
            return store[ref]
        raise secmod.SecretNotFoundError(str(ref))

    monkeypatch.setattr(secmod, "store_secret", fake_store)
    monkeypatch.setattr(secmod, "resolve_secret", fake_resolve)
    legacy = OAuthToken(access="LEG", refresh="LR", expires=99, account_id="la")
    monkeypatch.setattr(cda, "_kit_file_storage", lambda: types.SimpleNamespace(load=lambda: legacy))

    s = cda._CodexSecretsStorage()
    loaded = s.load()  # secret absent -> migrate from kit file
    assert loaded.access == "LEG"
    assert saved  # migration persisted into the secret store
    # legacy source no longer consulted now that the secret exists
    monkeypatch.setattr(cda, "_kit_file_storage", lambda: types.SimpleNamespace(load=lambda: None))
    assert s.load().access == "LEG"
