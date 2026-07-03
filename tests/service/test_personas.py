import asyncio
import pytest
from durin.personas import seed_example_personas
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


def test_seeded_examples_appear_as_editable(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    seed_example_personas()
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    names = {p.name for p in res.personas}
    assert {"researcher", "engineer", "tutor-feynman", "tutor-socratic"} <= names
    # seeded examples are ordinary editable personas, not an immutable category
    assert all(p.builtin is False for p in res.personas)


def test_upsert_persists_and_lists(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    asyncio.run(svc.upsert_persona(PersonaUpsertCommand(name="acme", soul="default", model=None, description="mine"), _principal()))
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    acme = next(p for p in res.personas if p.name == "acme")
    assert acme.soul == "default" and acme.builtin is False


def test_seeded_example_is_deletable(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    seed_example_personas()
    asyncio.run(svc.delete_persona(PersonaDeleteCommand(name="researcher"), _principal()))
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert not any(p.name == "researcher" for p in res.personas)


def test_delete_unknown_persona_raises(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    with pytest.raises(DomainError):
        asyncio.run(svc.delete_persona(PersonaDeleteCommand(name="ghost"), _principal()))


def test_set_default_valid_null_and_invalid(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    asyncio.run(svc.upsert_persona(PersonaUpsertCommand(name="mine", soul="default"), _principal()))
    asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="mine"), _principal()))
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res.default == "mine"
    asyncio.run(svc.set_default(SetDefaultPersonaCommand(name=None), _principal()))
    res2 = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res2.default == "durin"  # cleared → the synthetic base default
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
    assert res_after.default == "durin"  # default persona deleted → base default
    assert not any(p.name == "acme" for p in res_after.personas)


def test_durin_persona_listed_last(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    last = res.personas[-1]
    assert last.name == "durin" and last.soul == "default" and last.model is None and last.builtin is False
    assert sum(1 for p in res.personas if p.name == "durin") == 1
    assert res.default == "durin"  # active default when nothing configured


def test_set_default_to_durin_clears_override(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    asyncio.run(svc.upsert_persona(PersonaUpsertCommand(name="mine", soul="default"), _principal()))
    asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="mine"), _principal()))
    out = asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="durin"), _principal()))
    assert out.default is None  # selecting the synthetic base persona clears the override
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res.default == "durin"


def test_legacy_configured_default_maps_to_durin(tmp_path, monkeypatch):
    # A config written before the rename may have agents.defaults.persona == "default"
    # stored literally (pre-rename installs). It must still resolve to "durin" in the
    # listing rather than surfacing the old internal name.
    from durin.config.loader import mutate_config

    svc = _svc(tmp_path, monkeypatch)
    mutate_config(lambda c: setattr(c.agents.defaults, "persona", "default"))
    res = asyncio.run(svc.list_personas(PersonaListQuery(), _principal()))
    assert res.default == "durin"


def test_upsert_reserved_name_rejected(tmp_path, monkeypatch):
    from durin.service.types import ValidationFailedError
    svc = _svc(tmp_path, monkeypatch)
    with pytest.raises(ValidationFailedError):
        asyncio.run(svc.upsert_persona(PersonaUpsertCommand(name="default", soul="default"), _principal()))
    with pytest.raises(ValidationFailedError):
        asyncio.run(svc.upsert_persona(PersonaUpsertCommand(name="durin", soul="default"), _principal()))
