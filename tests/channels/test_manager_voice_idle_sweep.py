"""The voice idle sweep drops an engine that sat unused, and then asks the
malloc janitor to trim at once: the engine's ~1GB goes back to glibc's
arenas, not to the OS, and would otherwise stay resident until the janitor's
next periodic pass, minutes later."""
import asyncio
import types

import pytest

import durin.channels.manager as mgr


class _Svc:
    def __init__(self, *, unloads: bool) -> None:
        self._unloads = unloads

    def unload_if_idle(self, idle_s: float) -> bool:
        return self._unloads


def _manager(*, stt_unloads: bool, tts_unloads: bool):
    m = mgr.ChannelManager.__new__(mgr.ChannelManager)
    m.transcription = _Svc(unloads=stt_unloads)
    m.speech_synthesis = _Svc(unloads=tts_unloads)
    m.config = types.SimpleNamespace(
        transcription=types.SimpleNamespace(idle_unload_s=900),
        tts=types.SimpleNamespace(idle_unload_s=900),
    )
    return m


async def _one_sweep(m, monkeypatch) -> None:
    """Run the sweeper for exactly one pass: its first sleep returns at once,
    the second ends the loop."""
    sleeps = 0

    async def _sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await m._voice_idle_sweeper()


@pytest.mark.asyncio
@pytest.mark.parametrize("stt, tts", [(True, False), (False, True), (True, True)])
async def test_an_unload_requests_a_trim(monkeypatch, stt, tts):
    requests: list[int] = []
    monkeypatch.setattr(
        "durin.service.wiring.request_malloc_trim", lambda: requests.append(1))
    await _one_sweep(_manager(stt_unloads=stt, tts_unloads=tts), monkeypatch)
    assert requests == [1]


@pytest.mark.asyncio
async def test_no_unload_requests_no_trim(monkeypatch):
    requests: list[int] = []
    monkeypatch.setattr(
        "durin.service.wiring.request_malloc_trim", lambda: requests.append(1))
    await _one_sweep(_manager(stt_unloads=False, tts_unloads=False), monkeypatch)
    assert requests == []
