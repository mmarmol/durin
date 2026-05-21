from unittest.mock import patch

from durin.providers.anthropic_provider import AnthropicProvider
from durin.providers.azure_openai_provider import AzureOpenAIProvider
from durin.providers.openai_compat_provider import OpenAICompatProvider


def test_openai_compat_disables_sdk_retries_by_default() -> None:
    # AsyncOpenAI is built lazily — inspect the stashed kwargs that the
    # `_client` property will pass at first use.
    provider = OpenAICompatProvider(api_key="sk-test", default_model="gpt-4o")
    assert provider._client_kwargs["max_retries"] == 0


def test_anthropic_disables_sdk_retries_by_default() -> None:
    with patch("anthropic.AsyncAnthropic") as mock_client:
        AnthropicProvider(api_key="sk-test", default_model="claude-sonnet-4-5")

    kwargs = mock_client.call_args.kwargs
    assert kwargs["max_retries"] == 0


def test_azure_openai_disables_sdk_retries_by_default() -> None:
    with patch("durin.providers.azure_openai_provider.AsyncOpenAI") as mock_client:
        AzureOpenAIProvider(
            api_key="sk-test",
            api_base="https://example.openai.azure.com",
            default_model="gpt-4.1",
        )

    kwargs = mock_client.call_args.kwargs
    assert kwargs["max_retries"] == 0
