import asyncio

import pytest

from durin.voice.session import VoiceSession


def test_session_defaults():
    s = VoiceSession(chat_id="c1")
    assert s.chat_id == "c1"
    assert s.state == "idle"
    assert s.speak_task is None


@pytest.mark.asyncio
async def test_cancel_speak_cancels_and_clears():
    s = VoiceSession(chat_id="c1")

    async def _block():
        await asyncio.sleep(10)

    s.speak_task = asyncio.create_task(_block())
    await asyncio.sleep(0)  # let it start
    s.cancel_speak()
    assert s.speak_task is None
