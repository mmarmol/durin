import asyncio

from durin.service.personas import PersonasService, PersonaUpsertCommand
from durin.service.principal import Principal, Scope


def _principal():
    return Principal(
        subject="t",
        scopes=frozenset({Scope.CONFIG_READ.value, Scope.CONFIG_WRITE.value}),
        kind="local",
    )


def test_upsert_invokes_reload_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    called = {"n": 0}
    svc = PersonasService(
        workspace_resolver=lambda: tmp_path,
        on_config_changed=lambda: called.__setitem__("n", called["n"] + 1),
    )
    asyncio.run(
        svc.upsert_persona(
            PersonaUpsertCommand(name="qa", soul="default", model=None, description="x"),
            _principal(),
        )
    )
    assert called["n"] == 1


def test_delete_invokes_reload_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    called = {"n": 0}
    svc = PersonasService(
        workspace_resolver=lambda: tmp_path,
        on_config_changed=lambda: called.__setitem__("n", called["n"] + 1),
    )
    # create first so delete has something to remove
    asyncio.run(
        svc.upsert_persona(
            PersonaUpsertCommand(name="qa", soul="default", model=None, description="x"),
            _principal(),
        )
    )
    called["n"] = 0  # reset after upsert
    from durin.service.personas import PersonaDeleteCommand

    asyncio.run(svc.delete_persona(PersonaDeleteCommand(name="qa"), _principal()))
    assert called["n"] == 1


def test_set_default_invokes_reload_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    called = {"n": 0}
    svc = PersonasService(
        workspace_resolver=lambda: tmp_path,
        on_config_changed=lambda: called.__setitem__("n", called["n"] + 1),
    )
    asyncio.run(
        svc.upsert_persona(
            PersonaUpsertCommand(name="qa", soul="default", model=None, description="x"),
            _principal(),
        )
    )
    called["n"] = 0  # reset after upsert
    from durin.service.personas import SetDefaultPersonaCommand

    asyncio.run(svc.set_default(SetDefaultPersonaCommand(name="qa"), _principal()))
    assert called["n"] == 1


def test_no_hook_is_noop(tmp_path, monkeypatch):
    """PersonasService without a hook should work normally (backward compat)."""
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    svc = PersonasService(workspace_resolver=lambda: tmp_path)
    # should not raise
    asyncio.run(
        svc.upsert_persona(
            PersonaUpsertCommand(name="qa", soul="default", model=None, description="x"),
            _principal(),
        )
    )
