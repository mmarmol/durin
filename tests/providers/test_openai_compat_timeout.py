"""Timeout-config tests.

The OpenAI client is built lazily (per-event-loop) inside the provider's
``_client`` property. These tests verify the kwargs stashed at __init__
time are still right; the property exercises the same kwargs on first
access.
"""

from unittest.mock import patch, sentinel

from durin.providers.openai_compat_provider import OpenAICompatProvider
from durin.providers.registry import ProviderSpec


def _assert_openai_compat_timeout(timeout) -> None:
    assert timeout == 120.0


def test_openai_compat_provider_sets_sdk_timeout() -> None:
    provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
    _assert_openai_compat_timeout(provider._client_kwargs["timeout"])


def test_openai_compat_provider_sets_timeout_on_local_http_client() -> None:
    spec = ProviderSpec(
        name="local",
        keywords=(),
        env_key="",
        is_local=True,
        default_api_base="http://127.0.0.1:11434/v1",
    )
    with patch(
        "durin.providers.openai_compat_provider.httpx.AsyncClient",
        return_value=sentinel.http_client,
    ) as mock_http_client, patch(
        "durin.providers.openai_compat_provider.AsyncOpenAI"
    ) as mock_async_openai:
        provider = OpenAICompatProvider(spec=spec)
        # The lazy property fires AsyncOpenAI + httpx client construction.
        _ = provider._client

    client_kwargs = mock_http_client.call_args.kwargs
    _assert_openai_compat_timeout(client_kwargs["timeout"])
    assert client_kwargs["limits"].keepalive_expiry == 0

    openai_kwargs = mock_async_openai.call_args.kwargs
    _assert_openai_compat_timeout(openai_kwargs["timeout"])
    assert openai_kwargs["http_client"] is sentinel.http_client


def test_openai_compat_provider_timeout_can_be_overridden_by_env(monkeypatch) -> None:
    monkeypatch.setenv("DURIN_OPENAI_COMPAT_TIMEOUT_S", "45")
    provider = OpenAICompatProvider(api_key="test-key", api_base="https://example.com/v1")
    assert provider._client_kwargs["timeout"] == 45.0
