import asyncio

from durin.providers import codex_models


def test_parse_models_ranks_and_filters_hidden():
    data = {
        "models": [
            {"slug": "b-model", "priority": 2},
            {"slug": "a-model", "priority": 1},
            {"slug": "hidden-model", "priority": 0, "visibility": "hidden"},
        ]
    }
    assert codex_models._parse_models(data) == ["a-model", "b-model"]


def test_list_codex_models_async_falls_back_without_token():
    result = asyncio.run(codex_models.list_codex_models_async(None))
    assert result == list(codex_models.STATIC_FALLBACK)


def test_list_codex_models_async_uses_discovery(monkeypatch):
    async def fake_discover(token):
        return ["disco-1", "disco-2"]

    monkeypatch.setattr(codex_models, "_discover_async", fake_discover)
    result = asyncio.run(codex_models.list_codex_models_async("tok"))
    assert result == ["disco-1", "disco-2"]
