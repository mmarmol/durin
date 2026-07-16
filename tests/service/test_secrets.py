"""SP1: SecretsService — read/delete secret metadata (called directly)."""

import pytest

from durin.service.principal import Principal, Scope
from durin.service.secrets import (
    SecretDeleteCommand,
    SecretItem,
    SecretsListQuery,
    SecretsService,
    SecretStoreCommand,
)
from durin.service.types import ForbiddenError, NotFoundError, ValidationFailedError


@pytest.fixture()
def secrets_store(tmp_path, monkeypatch):
    """Point SecretStore at a tmp secrets.json holding one seeded secret."""
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )
    from durin.security.secrets import SecretStore

    store = SecretStore()
    store.put(
        "FOO_KEY",
        value="supersecretvalue123",
        service="svc",
        description="d",
        scope=["provider:foo"],
        origin="webui",
    )
    store.save()
    return tmp_path


async def test_list_returns_metadata_with_masked_hint(secrets_store):
    result = await SecretsService().list(SecretsListQuery(), Principal.local())
    assert len(result.secrets) == 1
    item = result.secrets[0]
    assert item.name == "FOO_KEY"
    assert item.service == "svc"
    assert item.scope == ["provider:foo"]
    assert item.origin == "webui"
    # The value never leaks — only a masked hint.
    assert item.value_hint == "supe••••e123"
    assert "supersecretvalue123" not in str(result.model_dump())


async def test_list_requires_read_scope(secrets_store):
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await SecretsService().list(SecretsListQuery(), principal)


async def test_delete_removes_secret(secrets_store):
    result = await SecretsService().delete(
        SecretDeleteCommand(name="FOO_KEY"), Principal.local()
    )
    assert result.ok is True
    listed = await SecretsService().list(SecretsListQuery(), Principal.local())
    assert listed.secrets == []


async def test_delete_unknown_raises_not_found(secrets_store):
    with pytest.raises(NotFoundError):
        await SecretsService().delete(
            SecretDeleteCommand(name="GHOST"), Principal.local()
        )


async def test_delete_requires_write_scope(secrets_store):
    principal = Principal.remote("t", frozenset({Scope.SECRETS_READ.value}))
    with pytest.raises(ForbiddenError):
        await SecretsService().delete(SecretDeleteCommand(name="FOO_KEY"), principal)


# --- store_entry / store tests ---


def test_store_entry_creates_new_secret(secrets_store):
    svc = SecretsService()
    item = svc.store_entry(
        name="MY_TOKEN",
        value="super-secret-value",
        service="github",
        scope=["exec"],
        origin="tui",
    )
    assert isinstance(item, SecretItem)
    assert item.name == "MY_TOKEN"
    assert item.service == "github"
    assert item.scope == ["exec"]
    assert item.origin == "tui"
    # account normalizes empty → None internally, surfaced as "" on the result.
    assert item.account == ""
    # The value is never returned — only a masked hint.
    assert item.value_hint == "supe••••alue"
    assert "super-secret-value" not in (item.value_hint or "")


def test_store_entry_normalizes_scope_whitespace(secrets_store):
    # scope controls secret visibility (collect_for), so whitespace/empties
    # must be stripped before they are stored.
    item = SecretsService().store_entry(
        name="MY_TOKEN",
        value="x" * 12,
        service="github",
        scope=["  exec  ", "", "  "],
    )
    assert item.scope == ["exec"]


def test_store_entry_metadata_only_edit_keeps_value(secrets_store):
    svc = SecretsService()
    svc.store_entry(name="MY_TOKEN", value="original", service="github", scope=["exec"])
    # Empty value on an existing secret = metadata-only edit (keep credential).
    item = svc.store_entry(name="MY_TOKEN", value="", service="gitlab", scope=["skill:deploy"])
    assert item.service == "gitlab"
    assert item.scope == ["skill:deploy"]
    # origin is preserved from the existing entry (first write used default "user").
    assert item.origin == "user"
    from durin.security.secrets import get_secret_store
    assert get_secret_store(reload=True).get("MY_TOKEN").value == "original"


def test_store_entry_rejects_empty_value_on_new(secrets_store):
    svc = SecretsService()
    with pytest.raises(ValidationFailedError, match="value is required"):
        svc.store_entry(name="BRAND_NEW", value="", service="github")


def test_store_entry_rejects_bad_name(secrets_store):
    svc = SecretsService()
    with pytest.raises(ValidationFailedError, match="invalid secret name"):
        svc.store_entry(name="lower-case", value="x" * 12, service="github")


def test_store_entry_rejects_missing_service(secrets_store):
    svc = SecretsService()
    with pytest.raises(ValidationFailedError, match="service is required"):
        svc.store_entry(name="MY_TOKEN", value="x" * 12, service="  ")


async def test_store_route_requires_write_scope(secrets_store):
    svc = SecretsService()
    cmd = SecretStoreCommand(name="MY_TOKEN", value="x" * 12, service="github")
    # A read-only principal must be rejected.
    read_only = Principal.remote("t1", frozenset({Scope.SECRETS_READ.value}))
    with pytest.raises(ForbiddenError):
        await svc.store(cmd, read_only)


async def test_store_route_writes_with_local_principal(secrets_store):
    svc = SecretsService()
    cmd = SecretStoreCommand(name="MY_TOKEN", value="x" * 12, service="github", scope=["exec"])
    item = await svc.store(cmd, Principal.local())
    assert item.name == "MY_TOKEN"
    assert item.scope == ["exec"]


# --- rotate (value-only replacement) tests ---


def test_store_entry_rotate_replaces_value_only(secrets_store):
    svc = SecretsService()
    svc.store_entry(
        name="GH", value="old-value-123", service="github", account="work",
        description="gh token", scope=["exec", "channel:telegram"],
    )
    item = svc.store_entry(name="GH", value="new-value-456", rotate=True)
    assert item.service == "github"
    assert item.account == "work"
    assert item.description == "gh token"
    assert item.scope == ["exec", "channel:telegram"]
    from durin.security.secrets import get_secret_store

    assert get_secret_store(reload=True).get("GH").value == "new-value-456"


def test_store_entry_rotate_ignores_incoming_metadata(secrets_store):
    svc = SecretsService()
    svc.store_entry(name="GH", value="old-value-123", service="github", scope=["exec"])
    svc.store_entry(
        name="GH", value="new-value-456", service="other", description="x",
        scope=["channel:slack"], rotate=True,
    )
    from durin.security.secrets import get_secret_store

    entry = get_secret_store(reload=True).get("GH")
    assert entry.service == "github"
    assert entry.scope == ["exec"]
    assert entry.description == ""


def test_store_entry_rotate_rejects_missing_secret(secrets_store):
    with pytest.raises(ValidationFailedError, match="no such secret"):
        SecretsService().store_entry(name="NOPE", value="v" * 12, rotate=True)


def test_store_entry_rotate_rejects_empty_value(secrets_store):
    svc = SecretsService()
    svc.store_entry(name="GH", value="old-value-123", service="github")
    with pytest.raises(ValidationFailedError, match="value is required"):
        svc.store_entry(name="GH", value="", rotate=True)
