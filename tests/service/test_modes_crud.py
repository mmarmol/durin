"""ModesService create/update/delete — custom modes persisted in config."""

import asyncio

import pytest

from durin.agent import agent_mode
from durin.service.modes import (
    ModeDeleteCommand,
    ModesListQuery,
    ModesService,
    ModeUpsertCommand,
)
from durin.service.principal import Principal, Scope
from durin.service.types import ForbiddenError, NotFoundError


def _principal():
    return Principal(
        subject="t",
        scopes=frozenset({Scope.SYSTEM_READ.value, Scope.CONFIG_WRITE.value}),
        kind="local",
    )


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    monkeypatch.setenv("DURIN_HOME", str(cfgdir))
    yield ModesService()
    agent_mode.register_config_modes({})  # reset registry to built-ins only


def _names(res):
    return {m["name"] for m in res.modes}


def test_upsert_persists_lists_and_registers(svc):
    asyncio.run(
        svc.upsert(
            ModeUpsertCommand(name="reviewer", description="reads", allowed=["read_file", "grep"]),
            _principal(),
        )
    )
    res = asyncio.run(svc.list(ModesListQuery(), _principal()))
    by = {m["name"]: m for m in res.modes}
    assert "reviewer" in by
    assert by["reviewer"]["builtin"] is False
    assert by["reviewer"]["allowed"] == ["grep", "read_file"]  # sorted projection
    # Registered live — the agent reads the registry, not config.
    assert agent_mode.get_mode("reviewer").is_tool_allowed("edit_file") is False
    assert agent_mode.get_mode("reviewer").is_tool_allowed("read_file") is True


def test_upsert_rejects_builtin_name(svc):
    with pytest.raises(ForbiddenError):
        asyncio.run(svc.upsert(ModeUpsertCommand(name="build", description="x"), _principal()))


def test_delete_removes_custom_mode(svc):
    asyncio.run(svc.upsert(ModeUpsertCommand(name="reviewer"), _principal()))
    asyncio.run(svc.delete(ModeDeleteCommand(name="reviewer"), _principal()))
    res = asyncio.run(svc.list(ModesListQuery(), _principal()))
    assert "reviewer" not in _names(res)
    assert {"build", "plan", "explore"} <= _names(res)


def test_delete_rejects_builtin(svc):
    with pytest.raises(ForbiddenError):
        asyncio.run(svc.delete(ModeDeleteCommand(name="plan"), _principal()))


def test_delete_unknown_raises_not_found(svc):
    with pytest.raises(NotFoundError):
        asyncio.run(svc.delete(ModeDeleteCommand(name="ghost"), _principal()))
