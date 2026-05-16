"""Tests for lazy provider exports from durin.providers."""

from __future__ import annotations

import importlib
import sys


def test_importing_providers_package_is_lazy(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "durin.providers", raising=False)
    monkeypatch.delitem(sys.modules, "durin.providers.anthropic_provider", raising=False)
    monkeypatch.delitem(sys.modules, "durin.providers.openai_compat_provider", raising=False)
    monkeypatch.delitem(sys.modules, "durin.providers.openai_codex_provider", raising=False)
    monkeypatch.delitem(sys.modules, "durin.providers.github_copilot_provider", raising=False)
    monkeypatch.delitem(sys.modules, "durin.providers.azure_openai_provider", raising=False)
    monkeypatch.delitem(sys.modules, "durin.providers.bedrock_provider", raising=False)

    providers = importlib.import_module("durin.providers")

    assert "durin.providers.anthropic_provider" not in sys.modules
    assert "durin.providers.openai_compat_provider" not in sys.modules
    assert "durin.providers.openai_codex_provider" not in sys.modules
    assert "durin.providers.github_copilot_provider" not in sys.modules
    assert "durin.providers.azure_openai_provider" not in sys.modules
    assert "durin.providers.bedrock_provider" not in sys.modules
    assert providers.__all__ == [
        "LLMProvider",
        "LLMResponse",
        "AnthropicProvider",
        "OpenAICompatProvider",
        "OpenAICodexProvider",
        "GitHubCopilotProvider",
        "AzureOpenAIProvider",
        "BedrockProvider",
    ]


def test_explicit_provider_import_still_works(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "durin.providers", raising=False)
    monkeypatch.delitem(sys.modules, "durin.providers.anthropic_provider", raising=False)

    namespace: dict[str, object] = {}
    exec("from durin.providers import AnthropicProvider", namespace)

    assert namespace["AnthropicProvider"].__name__ == "AnthropicProvider"
    assert "durin.providers.anthropic_provider" in sys.modules


