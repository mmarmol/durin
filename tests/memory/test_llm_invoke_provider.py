from durin.config.schema import Config, ModelPresetConfig
from durin.memory import llm_invoke as li
from durin.providers.base import LLMResponse as ProviderResponse


def test_aux_invoke_uses_resolved_provider_and_model(monkeypatch) -> None:
    seen: dict = {}

    class _FakeProvider:
        async def chat_with_retry(self, messages, tools=None, model=None, **kw):
            seen["model"] = model
            seen["prompt"] = messages[0]["content"]
            seen["tools"] = tools
            return ProviderResponse(content="ok", usage={"prompt_tokens": 7, "completion_tokens": 3})

    monkeypatch.setattr(
        "durin.providers.factory.make_provider", lambda cfg, *, preset: _FakeProvider()
    )
    preset = ModelPresetConfig(model="glm-5.2", provider="zai_coding_plan")
    out = li.aux_llm_invoke("hi", preset=preset, config=Config())

    assert out.text == "ok"
    assert out.prompt_tokens == 7
    assert out.completion_tokens == 3
    assert seen["model"] == "glm-5.2"  # the resolved model, NOT glm-5.1
    assert seen["prompt"] == "hi"
    assert seen["tools"] is None


def test_aux_invoke_empty_content_is_safe(monkeypatch) -> None:
    class _FakeProvider:
        async def chat_with_retry(self, messages, tools=None, model=None, **kw):
            return ProviderResponse(content=None)

    monkeypatch.setattr(
        "durin.providers.factory.make_provider", lambda cfg, *, preset: _FakeProvider()
    )
    out = li.aux_llm_invoke(
        "hi", preset=ModelPresetConfig(model="m", provider="p"), config=Config()
    )
    assert out.text == ""
