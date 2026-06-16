"""SP1: SecretsService — read/delete secret metadata (called directly)."""

import pytest

from durin.service.principal import Principal, Scope
from durin.service.secrets import (
    SecretDeleteCommand,
    SecretsListQuery,
    SecretsService,
)
from durin.service.types import ForbiddenError, NotFoundError


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
