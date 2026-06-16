"""SP5: build_api_app — write routes (POST/DELETE) with JSON bodies (TestClient)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_api_app
from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import AuthService
from durin.service.principal import Scope
from durin.service.registry import ServiceRegistry
from durin.service.secrets import SecretsService
from durin.service.settings import SettingsService

STATIC_TOKEN = "test-static-token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def token_store(tmp_path):
    return ApiTokenStore(path=tmp_path / "tokens.json")


@pytest.fixture()
def auth(token_store):
    return AuthService(store=token_store)


@pytest.fixture()
def secrets_store(tmp_path, monkeypatch):
    """Point SecretStore at a tmp file with one seeded secret."""
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )
    from durin.security.secrets import SecretStore

    store = SecretStore()
    store.put(
        "TO_DELETE",
        value="val",
        service="svc",
        description="desc",
        scope=["provider:svc"],
        origin="test",
    )
    store.save()
    return tmp_path


@pytest.fixture()
def config_env(tmp_path, monkeypatch):
    """Minimal config + secret store wired to tmp_path."""
    from durin.config.loader import save_config
    from durin.config.schema import Config

    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "openai/gpt-4o"
    config.providers.openai.api_key = "plain-openai-key"
    config.tools.web.search.provider = "duckduckgo"
    save_config(config, config_path)

    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )
    return tmp_path, config_path


@pytest.fixture()
def registry(auth, secrets_store):
    reg = ServiceRegistry()
    reg.register("secrets", SecretsService())
    reg.register("auth", auth)
    return reg


@pytest.fixture()
def settings_registry(auth, config_env):
    reg = ServiceRegistry()
    reg.register("settings", SettingsService())
    reg.register("auth", auth)
    return reg


@pytest.fixture()
def client(registry, auth):
    app = build_api_app(registry, auth=auth, static_token=STATIC_TOKEN)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def settings_client(settings_registry, auth):
    app = build_api_app(settings_registry, auth=auth, static_token=STATIC_TOKEN)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def write_token(token_store):
    _id, plaintext = token_store.issue([Scope.SECRETS_WRITE.value], label="write")
    return plaintext


@pytest.fixture()
def read_only_token(token_store):
    _id, plaintext = token_store.issue([Scope.SECRETS_READ.value], label="read-only")
    return plaintext


@pytest.fixture()
def settings_write_token(token_store):
    _id, plaintext = token_store.issue([Scope.SETTINGS_WRITE.value], label="settings-write")
    return plaintext


# ---------------------------------------------------------------------------
# DELETE write route — secrets delete
# ---------------------------------------------------------------------------


def test_delete_route_with_json_body_204(client):
    """DELETE /api/v1/secrets with JSON body deletes the secret."""
    r = client.request(
        "DELETE",
        "/api/v1/secrets",
        json={"name": "TO_DELETE"},
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


def test_delete_route_missing_token_401(client):
    r = client.request(
        "DELETE",
        "/api/v1/secrets",
        json={"name": "TO_DELETE"},
    )
    assert r.status_code == 401
    assert "problem+json" in r.headers["content-type"]


def test_delete_route_read_only_scope_403(client, read_only_token):
    """A token with only secrets:read must be denied on the delete route."""
    r = client.request(
        "DELETE",
        "/api/v1/secrets",
        json={"name": "TO_DELETE"},
        headers={"Authorization": f"Bearer {read_only_token}"},
    )
    assert r.status_code == 403
    body = r.json()
    assert body["status"] == 403
    assert "problem+json" in r.headers["content-type"]


def test_delete_route_write_token_allowed(client, write_token):
    r = client.request(
        "DELETE",
        "/api/v1/secrets",
        json={"name": "TO_DELETE"},
        headers={"Authorization": f"Bearer {write_token}"},
    )
    assert r.status_code == 200


def test_delete_not_found_returns_404_problem_json(client):
    """Deleting a non-existent secret returns 404 problem+json."""
    r = client.request(
        "DELETE",
        "/api/v1/secrets",
        json={"name": "DOES_NOT_EXIST"},
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
    )
    assert r.status_code == 404
    body = r.json()
    assert body["status"] == 404
    assert "problem+json" in r.headers["content-type"]


def test_get_on_delete_only_path_returns_405_or_404(client):
    """The old GET /api/v1/secrets/delete path is not mounted (404)."""
    r = client.get(
        "/api/v1/secrets/delete",
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
    )
    # The path was never registered — Starlette returns 404.
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST write route — settings provider update (leaker test)
# ---------------------------------------------------------------------------


def test_post_settings_provider_accepts_api_key_in_body(settings_client, config_env):
    """POST /api/v1/settings/provider with api_key in body stores it as a secret ref."""
    _, config_path = config_env
    r = settings_client.post(
        "/api/v1/settings/provider",
        json={"provider": "openai", "api_key": "sk-live-key-plain"},
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body

    # The plaintext key must NOT appear in the saved config — only a ${secret:} ref.
    from durin.config.loader import load_config

    saved = load_config(config_path)
    assert saved.providers.openai.api_key is not None
    assert "sk-live-key-plain" not in (saved.providers.openai.api_key or ""), (
        "Plaintext api_key must not be stored; must be a ${secret:} ref"
    )
    assert saved.providers.openai.api_key.startswith("${secret:"), (
        f"Expected secret ref, got: {saved.providers.openai.api_key!r}"
    )


def test_post_settings_provider_missing_scope_403(settings_client):
    """Settings provider update without settings:write scope → 403."""
    r = settings_client.post(
        "/api/v1/settings/provider",
        json={"provider": "openai", "api_key": "sk-plain"},
    )
    assert r.status_code == 401


def test_post_write_route_missing_write_scope_403(settings_client, token_store):
    """A token with only settings:read is denied on POST /api/v1/settings/provider."""
    _id, read_token = token_store.issue([Scope.SETTINGS_READ.value], label="r")
    r = settings_client.post(
        "/api/v1/settings/provider",
        json={"provider": "openai", "api_key": "sk-plain"},
        headers={"Authorization": f"Bearer {read_token}"},
    )
    assert r.status_code == 403
    body = r.json()
    assert body["status"] == 403
    assert "problem+json" in r.headers["content-type"]


def test_post_with_settings_write_token_allowed(settings_client, settings_write_token, config_env):
    r = settings_client.post(
        "/api/v1/settings/provider",
        json={"provider": "openai", "api_key": "sk-new"},
        headers={"Authorization": f"Bearer {settings_write_token}"},
    )
    assert r.status_code == 200


def test_api_key_not_in_url_query(settings_client, config_env):
    """The api_key must only travel in the JSON body, never in query params.

    A GET on the old path /api/v1/settings/provider/update is 404 (path removed).
    """
    r = settings_client.get(
        "/api/v1/settings/provider/update",
        params={"provider": "openai", "api_key": "sk-secret"},
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
    )
    # Old GET path was removed — must be 404, proving the key can't leak via URL.
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST body — empty body graceful handling (DELETE with no body)
# ---------------------------------------------------------------------------


def test_delete_with_empty_body_422_missing_required_field(client):
    """DELETE /api/v1/secrets with no body → 422 (name is required)."""
    r = client.request(
        "DELETE",
        "/api/v1/secrets",
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
    )
    assert r.status_code == 422
    assert "problem+json" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# POST write route — secrets store
# ---------------------------------------------------------------------------


def test_post_secrets_creates_and_returns_metadata(client):
    resp = client.post(
        "/api/v1/secrets",
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
        json={"name": "API_PLAN_TOKEN", "value": "value-1234-5678", "service": "github", "scope": ["exec"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "API_PLAN_TOKEN"
    assert body["service"] == "github"
    assert body["scope"] == ["exec"]
    # snake_case on the wire (consistent with the rest of /api/v1), value never present.
    assert "value_hint" in body
    assert "value" not in body


def test_post_secrets_rejects_bad_name_422(client):
    resp = client.post(
        "/api/v1/secrets",
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
        json={"name": "bad-name", "value": "value-1234-5678", "service": "github"},
    )
    assert resp.status_code == 422


def test_post_secrets_requires_auth_401(client):
    resp = client.post(
        "/api/v1/secrets",
        json={"name": "API_PLAN_TOKEN", "value": "value-1234-5678", "service": "github"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Read routes still work (reads unchanged)
# ---------------------------------------------------------------------------


def test_read_route_still_mounted(client):
    """GET /api/v1/secrets still returns 200 (reads unchanged)."""
    r = client.get(
        "/api/v1/secrets",
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "secrets" in body
