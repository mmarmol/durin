import pytest

from durin.memory import llm_invoke


def _transient():
    return Exception("litellm.InternalServerError: OpenAIException - Connection error.")


def _fatal():
    return Exception("AuthenticationError: invalid api key")


def test_retry_recovers_from_transient(monkeypatch):
    monkeypatch.setattr(llm_invoke.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _transient()
        return "ok"

    assert llm_invoke._retry_llm_call(call, mode="standard") == "ok"
    assert calls["n"] == 3


def test_retry_does_not_retry_fatal(monkeypatch):
    monkeypatch.setattr(llm_invoke.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise _fatal()

    with pytest.raises(Exception, match="AuthenticationError"):
        llm_invoke._retry_llm_call(call, mode="standard")
    assert calls["n"] == 1


def test_standard_gives_up_after_full_schedule(monkeypatch):
    monkeypatch.setattr(llm_invoke.time, "sleep", lambda _s: None)
    from durin.providers.base import LLMProvider
    attempts = len(LLMProvider._CHAT_RETRY_DELAYS) + 1  # initial + each delay

    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise _transient()

    with pytest.raises(Exception, match="Connection error"):
        llm_invoke._retry_llm_call(call, mode="standard")
    assert calls["n"] == attempts
