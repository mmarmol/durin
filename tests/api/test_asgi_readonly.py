"""SP4: build_api_app — read-only Starlette front door (TestClient)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_api_app
from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import AuthService
from durin.service.principal import Scope
from durin.service.registry import ServiceRegistry
from durin.service.secrets import SecretsService

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
        "API_KEY",
        value="plainvalue",
        service="svc",
        description="desc",
        scope=["provider:svc"],
        origin="webui",
    )
    store.save()
    return tmp_path


@pytest.fixture()
def registry(auth):
    reg = ServiceRegistry()
    reg.register("secrets", SecretsService())
    reg.register("auth", auth)
    return reg


@pytest.fixture()
def client(registry, auth, secrets_store):
    app = build_api_app(registry, auth=auth, static_token=STATIC_TOKEN)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def read_token(token_store):
    _id, plaintext = token_store.issue([Scope.SECRETS_READ.value], label="read")
    return plaintext


@pytest.fixture()
def write_only_token(token_store):
    """Token with write scope only — should be denied on read routes."""
    _id, plaintext = token_store.issue([Scope.SECRETS_WRITE.value], label="write")
    return plaintext


# ---------------------------------------------------------------------------
# Health endpoint — no auth required
# ---------------------------------------------------------------------------


def test_health_no_auth(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_with_auth_still_200(client):
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {STATIC_TOKEN}"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Read route — GET /api/v1/secrets — happy path
# ---------------------------------------------------------------------------


def test_read_route_with_static_token_200(client):
    r = client.get("/api/v1/secrets", headers={"Authorization": f"Bearer {STATIC_TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert "secrets" in body
    assert body["secrets"][0]["name"] == "API_KEY"


def test_read_route_with_persisted_read_token_200(client, read_token):
    r = client.get("/api/v1/secrets", headers={"Authorization": f"Bearer {read_token}"})
    assert r.status_code == 200
    assert "secrets" in r.json()


# ---------------------------------------------------------------------------
# Auth failures
# ---------------------------------------------------------------------------


def test_missing_token_returns_401(client):
    r = client.get("/api/v1/secrets")
    assert r.status_code == 401
    body = r.json()
    assert body["status"] == 401
    assert "type" in body
    # RFC-9457 content type
    assert "problem+json" in r.headers["content-type"]


def test_wrong_token_returns_401(client):
    r = client.get("/api/v1/secrets", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_token_lacking_scope_returns_403(client, write_only_token):
    """A token with only secrets:write — not secrets:read — gets 403."""
    r = client.get("/api/v1/secrets", headers={"Authorization": f"Bearer {write_only_token}"})
    assert r.status_code == 403
    body = r.json()
    assert body["status"] == 403
    assert "problem+json" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# Write route NOT mounted on read-only app
# ---------------------------------------------------------------------------


def test_write_route_not_mounted(client):
    """GET /api/v1/secrets/delete is a write route (secrets:write) — must be 404."""
    r = client.get(
        "/api/v1/secrets/delete",
        headers={"Authorization": f"Bearer {STATIC_TOKEN}"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DomainError → problem+json
# ---------------------------------------------------------------------------


def test_not_found_error_returns_404_problem_json(auth, secrets_store):
    """A route that raises NotFoundError must return 404 problem+json.

    We subclass SecretsService and copy the parent's __route_spec__ so the
    overriding method is picked up by the registry.
    """
    from durin.service.registry import ROUTE_ATTR
    from durin.service.secrets import SecretsListQuery, SecretsService
    from durin.service.types import NotFoundError

    async def _raise(self, query: SecretsListQuery, principal):
        raise NotFoundError("test not found")

    # Copy the @route spec from the parent so the registry can find this method.
    setattr(_raise, ROUTE_ATTR, getattr(SecretsService.list, ROUTE_ATTR))

    class _RaisingSecrets(SecretsService):
        list = _raise  # type: ignore[assignment]

    reg = ServiceRegistry()
    reg.register("secrets", _RaisingSecrets())

    app = build_api_app(reg, auth=auth, static_token=STATIC_TOKEN)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/v1/secrets", headers={"Authorization": f"Bearer {STATIC_TOKEN}"})
    assert r.status_code == 404
    body = r.json()
    assert body["status"] == 404
    assert body["detail"] == "test not found"
    assert "problem+json" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# Request-ID middleware
# ---------------------------------------------------------------------------


def test_request_id_header_propagated(client):
    r = client.get("/api/v1/health")
    assert "x-request-id" in r.headers


def test_request_id_forwarded_when_provided(client):
    r = client.get("/api/v1/health", headers={"X-Request-Id": "my-id-123"})
    assert r.headers["x-request-id"] == "my-id-123"
