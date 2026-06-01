from durin.utils.helpers import estimate_prompt_tokens_chain


class _NoCounterProvider:
    pass


class _BrokenCounterProvider:
    def estimate_prompt_tokens(self, messages, tools=None, model=None):
        raise RuntimeError("counter unavailable")


def test_estimate_prompt_tokens_chain_falls_back_without_provider_counter() -> None:
    tokens, source = estimate_prompt_tokens_chain(
        _NoCounterProvider(),
        "test-model",
        [{"role": "user", "content": "hello"}],
    )

    assert tokens > 0
    assert source == "tiktoken"


def test_estimate_prompt_tokens_chain_falls_back_when_provider_counter_fails() -> None:
    tokens, source = estimate_prompt_tokens_chain(
        _BrokenCounterProvider(),
        "test-model",
        [{"role": "user", "content": "hello"}],
    )

    assert tokens > 0
    assert source == "tiktoken"


def test_estimate_prompt_tokens_does_not_collapse_to_zero_on_unserializable_payload() -> None:
    """C3: a quirky-but-non-empty payload must not estimate to 0.

    The old over-broad try wrapped the whole build loop, so a tool_calls
    value that ``json.dumps`` couldn't serialize raised inside it and the
    handler returned 0 — indistinguishable from an empty prompt, silently
    corrupting budget/telemetry arithmetic. The estimate must stay > 0.
    """
    from durin.utils.helpers import estimate_prompt_tokens

    messages = [
        {"role": "user", "content": "a reasonably long user message here"},
        # A set is not JSON-serializable by default — exercised the swallow.
        {"role": "assistant", "tool_calls": [{"id": "x", "args": {1, 2, 3}}]},
    ]
    assert estimate_prompt_tokens(messages) > 0
