"""Signature conformance: every concrete provider's chat/chat_stream must
accept the exact kwargs ``chat_with_retry`` passes.

A narrower override turns EVERY call through the retry wrapper into a
``TypeError``-as-error-response on the first attempt (non-transient, never
retried into success). This happened: new sampling params were added to the
wrapper while five native overrides kept their old signatures, breaking those
providers entirely — and the suite stayed green because the test fakes accept
broad kwargs. This test runs against the real classes so a signature drift
fails loudly.
"""
from __future__ import annotations

import inspect

import pytest

from durin.providers.anthropic_provider import AnthropicProvider
from durin.providers.azure_openai_provider import AzureOpenAIProvider
from durin.providers.bedrock_provider import BedrockProvider
from durin.providers.github_copilot_provider import GitHubCopilotProvider
from durin.providers.local_llama_provider import LocalLlamaProvider
from durin.providers.openai_codex_provider import OpenAICodexProvider
from durin.providers.openai_compat_provider import OpenAICompatProvider

# The kw dict chat_with_retry / chat_stream_with_retry always pass through
# _run_with_retry to _safe_chat_stream -> chat_stream (see base.py).
RETRY_KW = {
    "messages", "tools", "model", "max_tokens", "temperature",
    "reasoning_effort", "tool_choice", "top_p", "top_k",
    "repeat_penalty", "extra_body",
}

PROVIDERS = [
    AnthropicProvider,
    AzureOpenAIProvider,
    BedrockProvider,
    GitHubCopilotProvider,
    LocalLlamaProvider,
    OpenAICodexProvider,
    OpenAICompatProvider,
]


def _missing(fn, names: set[str]) -> set[str]:
    sig = inspect.signature(fn)
    params = sig.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return set()
    return names - set(sig.parameters)


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.__name__)
def test_chat_accepts_retry_wrapper_kwargs(cls) -> None:
    missing = _missing(cls.chat, RETRY_KW)
    assert not missing, f"{cls.__name__}.chat rejects retry-wrapper kwargs: {sorted(missing)}"


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.__name__)
def test_chat_stream_accepts_retry_wrapper_kwargs(cls) -> None:
    missing = _missing(cls.chat_stream, RETRY_KW)
    assert not missing, f"{cls.__name__}.chat_stream rejects retry-wrapper kwargs: {sorted(missing)}"
