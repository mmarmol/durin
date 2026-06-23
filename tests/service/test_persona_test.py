import asyncio
import pytest
from durin.service.personas import PersonasService, PersonaTestCommand
from durin.service.principal import Principal, Scope


def _principal():
    return Principal(subject="t", scopes=frozenset({Scope.CONFIG_READ.value}), kind="local")


class _FakeResp:
    def __init__(self, content, finish_reason="stop"):
        self.content = content
        self.finish_reason = finish_reason
        self.tool_calls = []
        self.usage = {}


class _FakeProvider:
    def __init__(self, content=None, exc=None, finish_reason="stop"):
        self._content = content
        self._exc = exc
        self._finish_reason = finish_reason

    async def chat_with_retry(self, **kw):
        if self._exc:
            raise self._exc
        return _FakeResp(self._content, finish_reason=self._finish_reason)


def _svc(tmp_path, monkeypatch, provider):
    (tmp_path / "SOUL.md").write_text("# Soul\nDefault voice.", encoding="utf-8")
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.providers.factory.make_provider", lambda *a, **k: provider)
    return PersonasService(workspace_resolver=lambda: tmp_path)


def test_ok_returns_reply(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, _FakeProvider(content="Hi there!"))
    res = asyncio.run(svc.test_persona(PersonaTestCommand(model=None, soul="default"), _principal()))
    assert res.ok is True and res.reply == "Hi there!"


def test_provider_error_is_returned_not_raised(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, _FakeProvider(exc=RuntimeError("401 auth")))
    res = asyncio.run(svc.test_persona(PersonaTestCommand(model="openai gpt-4o", soul=None), _principal()))
    assert res.ok is False and "401 auth" in (res.error or "")


def test_empty_response_is_not_ok(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, _FakeProvider(content=None))
    res = asyncio.run(svc.test_persona(PersonaTestCommand(model=None, soul=None), _principal()))
    assert res.ok is False and res.error


def test_finish_reason_error_is_not_ok(tmp_path, monkeypatch):
    provider = _FakeProvider(content="Error calling LLM: 401 Unauthorized", finish_reason="error")
    svc = _svc(tmp_path, monkeypatch, provider)
    res = asyncio.run(svc.test_persona(PersonaTestCommand(model=None, soul=None), _principal()))
    assert res.ok is False
    assert "401" in (res.error or "")


def test_bad_soul_slug_does_not_500(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch, _FakeProvider(content="hi"))
    res = asyncio.run(svc.test_persona(PersonaTestCommand(model=None, soul="../bad"), _principal()))
    assert res.ok is True
