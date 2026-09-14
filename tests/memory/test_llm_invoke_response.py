from durin.memory.llm_invoke import LLMResponse


def test_the_dream_reply_answers_to_both_names():
    """``text`` is the field; ``content`` is the same string under the name the
    provider layer's response uses, so a consumer written against either shape
    reads the answer instead of nothing."""
    resp = LLMResponse(text="the answer", finish_reason="stop")
    assert resp.content == "the answer"
    assert getattr(resp, "content", "") == resp.text
