"""Hardening of the OpenAI-compatible request path.

Covers three fixes shaped by how real providers (GLM/Z.AI, DeepSeek) behave:

1. Assistant ``content`` is kept alongside ``tool_calls`` — blanking it hid the
   model's own narration across tool steps, so models like GLM re-narrated the
   same acknowledgment on every step.
2. Lone UTF-16 surrogates emitted by byte-level reasoning models are scrubbed
   before the request is UTF-8 encoded (they would otherwise crash the call).
3. DeepSeek thinking-mode ``reasoning_content`` is padded with a single space,
   not an empty string (DeepSeek V4 Pro rejects ``""``).
"""

from unittest.mock import patch

from durin.providers.base import LLMResponse
from durin.providers.openai_compat_provider import _OMIT, OpenAICompatProvider
from durin.providers.registry import ProviderSpec


def _provider() -> OpenAICompatProvider:
    with patch("durin.providers.openai_compat_provider.AsyncOpenAI"):
        return OpenAICompatProvider()


# ── 1. content + tool_calls is preserved ─────────────────────────────────


def test_assistant_content_kept_with_tool_calls() -> None:
    provider = _provider()
    messages = [
        {"role": "user", "content": "trace this"},
        {
            "role": "assistant",
            "content": "You're right — the dedup destroys a critical signal.",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "..."},
    ]

    out = provider._sanitize_messages(messages)

    assistant = next(m for m in out if m["role"] == "assistant")
    assert assistant["content"] == "You're right — the dedup destroys a critical signal."
    assert assistant["tool_calls"], "tool_calls must survive sanitization"


# ── 2. surrogate scrubbing ───────────────────────────────────────────────


def test_lone_surrogates_scrubbed_from_content_and_reasoning() -> None:
    provider = _provider()
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "hola\ud83dmundo",  # lone high surrogate
            "reasoning_content": "think\udc00ing",  # lone low surrogate
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": '{"x": "a\ud834b"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "f", "content": "ok"},
    ]

    out = provider._sanitize_messages(messages)
    msg = next(m for m in out if m["role"] == "assistant")

    # Scrubbed content must survive a UTF-8 encode (the real failure mode).
    msg["content"].encode("utf-8")
    msg["reasoning_content"].encode("utf-8")
    msg["tool_calls"][0]["function"]["arguments"].encode("utf-8")
    assert "�" in msg["content"]
    assert "�" in msg["reasoning_content"]


def test_clean_content_untouched_by_surrogate_scrub() -> None:
    provider = _provider()
    out = provider._sanitize_messages(
        [{"role": "user", "content": "normal text — nothing to scrub"}]
    )
    assert out[0]["content"] == "normal text — nothing to scrub"


# ── 3. DeepSeek reasoning_content padding ─────────────────────────────────


def _deepseek_provider() -> OpenAICompatProvider:
    spec = ProviderSpec(name="deepseek", keywords=("deepseek",), env_key="DEEPSEEK_API_KEY")
    with patch("durin.providers.openai_compat_provider.AsyncOpenAI"):
        return OpenAICompatProvider(spec=spec)


def test_deepseek_reasoning_pad_is_space_not_empty() -> None:
    provider = _deepseek_provider()
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "f", "content": "ok"},
    ]

    kwargs = provider._build_kwargs(
        messages, tools=None, model="deepseek-v4", max_tokens=100,
        temperature=0.7, reasoning_effort="high", tool_choice=None,
    )

    assistant = next(m for m in kwargs["messages"] if m.get("role") == "assistant")
    assert assistant["reasoning_content"] == " ", "empty string 400s on DeepSeek V4 Pro"


def test_deepseek_reasoning_pad_upgrades_legacy_empty_string() -> None:
    provider = _deepseek_provider()
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "",  # legacy pin persisted before the fix
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "f", "content": "ok"},
    ]

    kwargs = provider._build_kwargs(
        messages, tools=None, model="deepseek-v4", max_tokens=100,
        temperature=0.7, reasoning_effort="high", tool_choice=None,
    )

    assistant = next(m for m in kwargs["messages"] if m.get("role") == "assistant")
    assert assistant["reasoning_content"] == " "


# ── 4. reactive request recovery (strip-on-400) ──────────────────────────


def _error(content: str, status: int = 400) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="error", error_status_code=status)


def test_recover_blanks_content_on_mixed_content_tool_calls_rejection() -> None:
    provider = _provider()
    kw = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "narration",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "f", "content": "ok"},
        ],
        "temperature": 0.7,
    }
    resp = _error("Invalid: assistant message content must be null when tool_calls are present")

    recovered = provider._recover_request_for_error(kw, resp)

    assert recovered is not None
    asst = next(m for m in recovered["messages"] if m["role"] == "assistant")
    assert asst["content"] is None
    # original kw is not mutated
    assert kw["messages"][1]["content"] == "narration"


def test_recover_strips_temperature_when_rejected() -> None:
    provider = _provider()
    kw = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.7, "max_tokens": 100}
    resp = _error("Unsupported value: 'temperature' does not support 0.7 with this model")

    recovered = provider._recover_request_for_error(kw, resp)

    assert recovered is not None
    assert recovered["temperature"] is _OMIT


def test_recover_strips_max_tokens_when_rejected() -> None:
    provider = _provider()
    kw = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.7, "max_tokens": 100}
    resp = _error("Unsupported parameter: 'max_tokens' is not supported; use max_completion_tokens")

    recovered = provider._recover_request_for_error(kw, resp)

    assert recovered is not None
    assert recovered["max_tokens"] is _OMIT


def test_recover_returns_none_for_unrelated_error() -> None:
    provider = _provider()
    kw = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.7}
    assert provider._recover_request_for_error(kw, _error("401 unauthorized", status=401)) is None


def test_omitted_temperature_and_max_tokens_are_not_sent() -> None:
    provider = _provider()
    kwargs = provider._build_kwargs(
        [{"role": "user", "content": "hi"}], tools=None, model="glm-5.2",
        max_tokens=_OMIT, temperature=_OMIT, reasoning_effort=None, tool_choice=None,
    )
    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs
    assert "max_completion_tokens" not in kwargs
