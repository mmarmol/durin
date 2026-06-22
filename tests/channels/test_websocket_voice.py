import json

import pytest

from durin.channels.websocket import WebSocketChannel


class _FakeConn:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, raw: str):
        self.sent.append(raw)

    @property
    def remote(self):
        return "test"


def _voice_channel():
    ch = WebSocketChannel.__new__(WebSocketChannel)
    ch.logger = __import__("loguru").logger
    ch._voice = {}
    ch._subs = {}
    return ch


@pytest.mark.asyncio
async def test_voice_start_creates_session_and_emits_listening():
    ch = _voice_channel()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    await ch._dispatch_envelope(conn, "client1", {"type": "voice_start", "chat_id": "c1"})
    assert "c1" in ch._voice
    events = [json.loads(r) for r in conn.sent]
    assert any(e.get("event") == "voice_state" and e.get("state") == "listening" for e in events)


@pytest.mark.asyncio
async def test_voice_stop_removes_session_and_emits_idle():
    ch = _voice_channel()
    conn = _FakeConn()
    ch._subs["c1"] = {conn}
    await ch._dispatch_envelope(conn, "client1", {"type": "voice_start", "chat_id": "c1"})
    await ch._dispatch_envelope(conn, "client1", {"type": "voice_stop", "chat_id": "c1"})
    assert "c1" not in ch._voice
    events = [json.loads(r) for r in conn.sent]
    assert any(e.get("event") == "voice_state" and e.get("state") == "idle" for e in events)
