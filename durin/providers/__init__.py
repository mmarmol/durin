"""LLM provider abstraction module."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from durin.providers.base import LLMProvider, LLMResponse

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "AnthropicProvider",
    "OpenAICompatProvider",
    "OpenAICodexProvider",
    "GitHubCopilotProvider",
    "AzureOpenAIProvider",
    "BedrockProvider",
]

_LAZY_IMPORTS = {
    "AnthropicProvider": ".anthropic_provider",
    "OpenAICompatProvider": ".openai_compat_provider",
    "OpenAICodexProvider": ".openai_codex_provider",
    "GitHubCopilotProvider": ".github_copilot_provider",
    "AzureOpenAIProvider": ".azure_openai_provider",
    "BedrockProvider": ".bedrock_provider",
}

if TYPE_CHECKING:
    from durin.providers.anthropic_provider import AnthropicProvider
    from durin.providers.azure_openai_provider import AzureOpenAIProvider
    from durin.providers.bedrock_provider import BedrockProvider
    from durin.providers.github_copilot_provider import GitHubCopilotProvider
    from durin.providers.openai_codex_provider import OpenAICodexProvider
    from durin.providers.openai_compat_provider import OpenAICompatProvider


def __getattr__(name: str):
    """Lazily expose provider implementations without importing all backends up front.

    Also falls back to importing *name* as a real submodule, so plain attribute
    access (``durin.providers.factory``) resolves even before an explicit import.
    pytest's ``monkeypatch.setattr("durin.providers.<submodule>.<attr>", …)``
    walks the package this way; without the fallback it hit the AttributeError
    below whenever the submodule was not yet bound.
    """
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is not None:
        module = import_module(module_name, __name__)
        return getattr(module, name)
    if not name.startswith("_"):
        try:
            return import_module(f".{name}", __name__)
        except ImportError:
            pass
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
