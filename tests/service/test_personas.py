import asyncio
import pytest
from durin.service.personas import (
    PersonasService, PersonaListQuery, PersonaUpsertCommand,
    PersonaDeleteCommand, SetDefaultPersonaCommand,
)
from durin.service.principal import Principal, Scope
from durin.service.types import DomainError


def _principal():
    return Principal(subject="t", scopes=frozenset({Scope.CONFIG_READ.value, Scope.CONFIG_WRITE.value}), kind="local")


def _svc(tmp_path, monkeypatch):
    cfgdir = tmp_path / "cfg"; cfgdir.mkdir()
    monkeypatch.setenv("DURIN_HOME", str(cfgdir))
    return PersonasService(workspace_resolver=lambda: tmp_path)


def test_list_includes_builtins_and_default(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    names = {p.name for p in res.personas}
    assert {"researcher", "engineer", "tutor"} <= names
    assert any(p.builtin for p in res.personas)


def test_upsert_persists_and_lists(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    asyncio.run(svc.upsert_persona(PersonaUpsertCommand(name="acme", soul="default", model=None, description="mine"), _principal()))
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    acme = next(p for p in res.personas if p.name == "acme")
    assert acme.soul == "default" and acme.builtin is False


def test_delete_builtin_rejected(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    with pytest.raises(DomainError):
        asyncio.run(svc.delete_persona(PersonaDeleteCommand(name="researcher"), _principal()))


def test_set_default_valid_null_and_invalid(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="engineer"), _principal()))
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res.default == "engineer"
    asyncio.run(svc.set_default(SetDefaultPersonaCommand(name=None), _principal()))
    res2 = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res2.default is None
    with pytest.raises(DomainError):
        asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="ghost"), _principal()))
