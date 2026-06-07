import asyncio

from durin.memory import llm_invoke


class _FakeChunk:
    def __init__(self, rc=None, c=None):
        self.choices = [
            type("C", (), {"delta": type("D", (), {"reasoning_content": rc, "content": c})()})()
        ]


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for ch in self._chunks:
                yield ch

        return gen()


def _fake_store():
    return type("S", (), {"get": staticmethod(lambda _k: type("E", (), {"value": "k"})())})()


def test_astream_forwards_reasoning_and_assembles_text(monkeypatch):
    chunks = [_FakeChunk(rc="think"), _FakeChunk(rc="ing"), _FakeChunk(c="ans"), _FakeChunk(c="wer")]

    async def fake_acompletion(**kwargs):
        assert kwargs["stream"] is True
        return _FakeStream(chunks)

    monkeypatch.setattr("durin.security.secrets.get_secret_store", _fake_store)
    monkeypatch.setattr(llm_invoke, "_acompletion", fake_acompletion)
    seen = []

    async def go():
        return await llm_invoke.default_llm_invoke_astream(
            "p", model="m", on_reasoning=lambda s: seen.append(s), on_content=None
        )

    text = asyncio.run(go())
    assert text == "answer"
    assert "".join(seen) == "thinking"
