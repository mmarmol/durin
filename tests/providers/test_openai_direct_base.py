"""`_is_direct_openai_base` must compare the parsed host exactly, so a
look-alike domain or a path segment cannot spoof a direct-OpenAI match."""

from durin.providers.openai_compat_provider import _is_direct_openai_base


def test_direct_openai_bases_are_recognized():
    assert _is_direct_openai_base(None) is True
    assert _is_direct_openai_base("https://api.openai.com/v1") is True
    assert _is_direct_openai_base("https://api.openai.com") is True
    assert _is_direct_openai_base("api.openai.com") is True


def test_lookalike_and_path_spoofs_are_rejected():
    # substring-in-path must NOT match
    assert _is_direct_openai_base("https://evil.com/api.openai.com") is False
    # look-alike subdomain suffix must NOT match
    assert _is_direct_openai_base("https://api.openai.com.evil.com") is False


def test_other_gateways_are_not_direct():
    assert _is_direct_openai_base("https://openrouter.ai/api/v1") is False
    assert _is_direct_openai_base("https://my-host.openai.azure.com") is False
