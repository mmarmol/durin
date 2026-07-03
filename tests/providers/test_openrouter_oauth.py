import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

import httpx

from durin.providers import openrouter_oauth as oro


def _isolate_home(tmp_path, monkeypatch):
    """Point DURIN_HOME at tmp and drop the module-global secret-store cache
    so the test can't read a store loaded under a previous test's home."""
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    from durin.security.secrets import get_secret_store

    get_secret_store(reload=True)


def _mock_client(handler):
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)

    return factory


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = oro._gen_pkce()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert challenge == expected
    assert "=" not in verifier


def test_authorize_url_carries_callback_and_challenge():
    url = oro._build_authorize_url("http://127.0.0.1:9999/callback/abc", "CHAL")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert url.startswith(oro.AUTH_URL)
    assert qs["callback_url"] == ["http://127.0.0.1:9999/callback/abc"]
    assert qs["code_challenge"] == ["CHAL"]
    assert qs["code_challenge_method"] == ["S256"]


def test_exchange_code_returns_key(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == oro.KEYS_URL
        body = json.loads(request.content)
        assert body == {
            "code": "code-1",
            "code_verifier": "ver-1",
            "code_challenge_method": "S256",
        }
        return httpx.Response(200, json={"key": "sk-or-v1-abc"})

    monkeypatch.setattr(oro, "_client", _mock_client(handler))
    assert oro.exchange_code("code-1", "ver-1") == "sk-or-v1-abc"


def test_exchange_code_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        oro, "_client", _mock_client(lambda r: httpx.Response(403, json={}))
    )
    try:
        oro.exchange_code("c", "v")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "403" in str(exc)


def test_exchange_code_raises_on_missing_key(monkeypatch):
    monkeypatch.setattr(
        oro, "_client", _mock_client(lambda r: httpx.Response(200, json={"key": ""}))
    )
    try:
        oro.exchange_code("c", "v")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "no key" in str(exc)


def test_callback_server_accepts_only_nonce_path_and_captures_code():
    result = oro._CallbackResult()
    srv, callback_url = oro._start_callback_server(result)
    try:
        # Wrong path (forged request from a local port-scan) → 404, no code.
        base = callback_url.rsplit("/callback/", 1)[0]
        try:
            urllib.request.urlopen(f"{base}/callback/forged?code=evil", timeout=5)
        except urllib.error.HTTPError as e:
            assert e.code == 404
        assert result.code is None

        # Real redirect → 200, code captured, done set.
        with urllib.request.urlopen(f"{callback_url}?code=good-code", timeout=5) as resp:
            assert resp.status == 200
        assert result.code == "good-code"
        assert result.done.is_set()
    finally:
        srv.shutdown()


def test_store_key_persists_secret_ref_in_config(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)

    oro.store_key("sk-or-v1-secret")

    from durin.config.loader import load_config
    from durin.security.secrets import resolve_secret

    config = load_config()
    assert config.providers.openrouter.api_key == "${secret:OPENROUTER_API_KEY}"
    assert resolve_secret(config.providers.openrouter.api_key) == "sk-or-v1-secret"

    status = oro.key_status()
    assert status.connected is True
    assert status.api_key_hint

    # Disconnect clears the config field and removes durin's own secret.
    assert oro.disconnect() is True
    assert load_config().providers.openrouter.api_key is None
    assert oro.key_status().connected is False


def test_disconnect_keeps_foreign_secret_refs(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    from durin.config.loader import load_config, save_config
    from durin.security.secrets import resolve_secret, store_secret

    store_secret(
        "MY_OWN_OR_KEY",
        "sk-or-v1-manual",
        service="provider:openrouter",
        scope=["provider:openrouter"],
    )
    config = load_config()
    config.providers.openrouter.api_key = "${secret:MY_OWN_OR_KEY}"
    save_config(config)

    assert oro.disconnect() is True
    assert load_config().providers.openrouter.api_key is None
    # The user's own secret is not durin-oauth-managed → left in the store.
    assert resolve_secret("${secret:MY_OWN_OR_KEY}") == "sk-or-v1-manual"
