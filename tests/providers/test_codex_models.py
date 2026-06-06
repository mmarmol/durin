import httpx

from durin.providers import codex_models as cm


def _mock_client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def test_fallback_when_no_token():
    assert cm.list_codex_models(access_token=None) == list(cm.STATIC_FALLBACK)


def test_discovery_sorts_by_priority_and_drops_hidden(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/codex/models")
        assert request.headers["Authorization"] == "Bearer T"
        assert request.headers["originator"] == "codex_cli_rs"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "gpt-5.5", "priority": 1},
                    {"slug": "secret", "priority": 0, "visibility": "hidden"},
                    {"slug": "gpt-5.4", "priority": 5},
                ]
            },
        )

    monkeypatch.setattr(cm, "_client", _mock_client(handler))
    assert cm.list_codex_models(access_token="T") == ["gpt-5.5", "gpt-5.4"]


def test_discovery_failure_falls_back(monkeypatch):
    monkeypatch.setattr(
        cm, "_client", _mock_client(lambda req: httpx.Response(500, json={}))
    )
    assert cm.list_codex_models(access_token="T") == list(cm.STATIC_FALLBACK)
