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
    assert res2.default == "default"  # cleared → the synthetic base default
    with pytest.raises(DomainError):
        asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="ghost"), _principal()))


def test_delete_clears_dangling_default(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    asyncio.run(svc.upsert_persona(PersonaUpsertCommand(name="acme", soul="default"), _principal()))
    asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="acme"), _principal()))
    res_before = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res_before.default == "acme"
    asyncio.run(svc.delete_persona(PersonaDeleteCommand(name="acme"), _principal()))
    res_after = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res_after.default == "default"  # default persona deleted → base default
    assert not any(p.name == "acme" for p in res_after.personas)


def test_default_persona_listed_last(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    last = res.personas[-1]
    assert last.name == "default" and last.soul == "default" and last.model is None and last.builtin is True
    assert sum(1 for p in res.personas if p.name == "default") == 1
    assert res.default == "default"  # active default when nothing configured


def test_set_default_to_default_clears_override(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="engineer"), _principal()))
    out = asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="default"), _principal()))
    assert out.default is None  # selecting the synthetic default clears the override
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res.default == "default"
