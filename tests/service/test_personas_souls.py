import asyncio
from pathlib import Path
import pytest
from durin.service.personas import (
    PersonasService, SoulUpsertCommand, SoulDeleteCommand, SoulListQuery,
)
from durin.service.principal import Principal, Scope


def _principal():
    return Principal(subject="test", scopes=frozenset({Scope.CONFIG_READ.value, Scope.CONFIG_WRITE.value}), kind="local")


def _svc(tmp_path):
    (tmp_path / "SOUL.md").write_text("# Soul\nDefault.", encoding="utf-8")
    return PersonasService(workspace_resolver=lambda: tmp_path)


def test_list_includes_default(tmp_path):
    svc = _svc(tmp_path)
    res = asyncio.run(svc.list_souls(SoulListQuery(), _principal()))
    slugs = {s.slug for s in res.souls}
    assert "default" in slugs


def test_upsert_and_readback(tmp_path):
    svc = _svc(tmp_path)
    asyncio.run(svc.upsert_soul(SoulUpsertCommand(slug="researcher", body="# Soul\nAnalyst."), _principal()))
    res = asyncio.run(svc.list_souls(SoulListQuery(), _principal()))
    body = {s.slug: s.body for s in res.souls}
    assert body["researcher"] == "# Soul\nAnalyst."


def test_delete(tmp_path):
    svc = _svc(tmp_path)
    asyncio.run(svc.upsert_soul(SoulUpsertCommand(slug="temp", body="x"), _principal()))
    asyncio.run(svc.delete_soul(SoulDeleteCommand(slug="temp"), _principal()))
    res = asyncio.run(svc.list_souls(SoulListQuery(), _principal()))
    assert "temp" not in {s.slug for s in res.souls}


def test_invalid_slug_rejected(tmp_path):
    from durin.service.types import ValidationFailedError
    svc = _svc(tmp_path)
    with pytest.raises(ValidationFailedError):
        asyncio.run(svc.upsert_soul(SoulUpsertCommand(slug="../evil", body="x"), _principal()))


def test_delete_default_rejected(tmp_path):
    from durin.service.types import ForbiddenError
    svc = _svc(tmp_path)
    with pytest.raises(ForbiddenError):
        asyncio.run(svc.delete_soul(SoulDeleteCommand(slug="default"), _principal()))


def test_delete_invalid_slug_rejected(tmp_path):
    from durin.service.types import ValidationFailedError
    svc = _svc(tmp_path)
    with pytest.raises(ValidationFailedError):
        asyncio.run(svc.delete_soul(SoulDeleteCommand(slug="../evil"), _principal()))


def test_delete_soul_in_use_blocked(tmp_path, monkeypatch):
    from durin.service.personas import PersonaUpsertCommand
    from durin.service.types import ConflictError
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    svc = _svc(tmp_path)
    svc._store().write("vibes", "# Soul\nx")
    asyncio.run(svc.upsert_persona(PersonaUpsertCommand(name="p1", soul="vibes"), _principal()))
    with pytest.raises(ConflictError):
        asyncio.run(svc.delete_soul(SoulDeleteCommand(slug="vibes"), _principal()))


def test_delete_unused_soul_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    svc = _svc(tmp_path)
    svc._store().write("unused", "# Soul\nx")
    asyncio.run(svc.delete_soul(SoulDeleteCommand(slug="unused"), _principal()))
    res = asyncio.run(svc.list_souls(SoulListQuery(), _principal()))
    assert "unused" not in {s.slug for s in res.souls}
