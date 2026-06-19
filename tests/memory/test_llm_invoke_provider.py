import asyncio

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


def test_aux_invoke_safe_inside_running_loop(monkeypatch) -> None:
    """The skill-audit tool calls the SYNC invoke from inside the async agent loop.
    A bare asyncio.run would raise; the thread fallback must keep it working."""

    class _FakeProvider:
        async def chat_with_retry(self, messages, tools=None, model=None, **kw):
            return ProviderResponse(content="ok", usage={})

    monkeypatch.setattr(
        "durin.providers.factory.make_provider", lambda cfg, *, preset: _FakeProvider()
    )

    async def driver():
        # call the synchronous helper from within a running event loop
        return li.aux_llm_invoke(
            "hi", preset=ModelPresetConfig(model="m", provider="p"), config=Config()
        )

    out = asyncio.run(driver())
    assert out.text == "ok"


def test_aux_astream_forwards_reasoning_and_assembles(monkeypatch) -> None:
    seen: dict = {}

    class _FakeProvider:
        async def chat_stream_with_retry(
            self, messages, tools=None, model=None,
            on_content_delta=None, on_thinking_delta=None, **kw,
        ):
            seen["model"] = model
            if on_thinking_delta:
                await on_thinking_delta("think")
            if on_content_delta:
                await on_content_delta("answer")
            return ProviderResponse(content="answer")

    monkeypatch.setattr(
        "durin.providers.factory.make_provider", lambda cfg, *, preset: _FakeProvider()
    )
    reasoning: list = []

    async def on_reasoning(t):
        reasoning.append(t)

    text = asyncio.run(
        li.aux_llm_invoke_astream(
            "p",
            preset=ModelPresetConfig(model="glm-5.2", provider="zai_coding_plan"),
            config=Config(),
            on_reasoning=on_reasoning,
            on_content=None,
        )
    )
    assert text == "answer"
    assert reasoning == ["think"]
    assert seen["model"] == "glm-5.2"


def _cfg(default_model="glm-5.2", default_provider="zai_coding_plan") -> Config:
    c = Config()
    c.agents.defaults.provider = default_provider
    c.agents.defaults.model = default_model
    return c


def test_default_llm_invoke_resolves_memory_preset(monkeypatch) -> None:
    """default_llm_invoke (purpose=memory) runs the user's default preset, not glm-5.1."""
    seen: dict = {}

    class _FakeProvider:
        async def chat_with_retry(self, messages, tools=None, model=None, **kw):
            seen["model"] = model
            return ProviderResponse(content="m-ok", usage={})

    monkeypatch.setattr("durin.config.loader.load_config", lambda: _cfg())
    monkeypatch.setattr(
        "durin.providers.factory.make_provider", lambda cfg, *, preset: _FakeProvider()
    )
    out = li.default_llm_invoke("hi")
    assert out.text == "m-ok"
    assert seen["model"] == "glm-5.2"  # default preset, NOT glm-5.1


def test_judge_llm_invoke_resolves_judge_model(monkeypatch) -> None:
    """judge_llm_invoke (purpose=judge) honors the configured judge model."""
    seen: dict = {}

    class _FakeProvider:
        async def chat_with_retry(self, messages, tools=None, model=None, **kw):
            seen["model"] = model
            return ProviderResponse(content="j-ok", usage={})

    c = _cfg()
    c.skills.security.llm_judge.model = "glm-4.6"
    monkeypatch.setattr("durin.config.loader.load_config", lambda: c)
    monkeypatch.setattr(
        "durin.providers.factory.make_provider", lambda cfg, *, preset: _FakeProvider()
    )
    out = li.judge_llm_invoke("hi")
    assert out.text == "j-ok"
    assert seen["model"] == "glm-4.6"


def test_judge_llm_invoke_falls_back_to_default_when_unset(monkeypatch) -> None:
    seen: dict = {}

    class _FakeProvider:
        async def chat_with_retry(self, messages, tools=None, model=None, **kw):
            seen["model"] = model
            return ProviderResponse(content="ok", usage={})

    monkeypatch.setattr("durin.config.loader.load_config", lambda: _cfg(default_model="claude-x"))
    monkeypatch.setattr(
        "durin.providers.factory.make_provider", lambda cfg, *, preset: _FakeProvider()
    )
    li.judge_llm_invoke("hi")
    assert seen["model"] == "claude-x"  # the user's default, never glm-5.1
