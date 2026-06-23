"""Per-chat_id voice-mode state held by the WebSocket channel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class VoiceSession:
    """Tracks one chat's voice mode: current state + the in-flight speak task."""

    chat_id: str
    state: str = "idle"  # idle | listening | transcribing | thinking | speaking
    speak_task: asyncio.Task | None = None
    # The assistant reply is streamed to the webui as deltas, not a single
    # message — accumulate them here so the FULL text can be spoken on stream end.
    reply_buffer: list[str] = field(default_factory=list)

    def cancel_speak(self) -> None:
        """Cancel any in-flight TTS task (barge-in / stop) and clear the ref."""
        task = self.speak_task
        self.speak_task = None
        if task is not None and not task.done():
            task.cancel()

    def take_reply(self) -> str:
        """Return the accumulated streamed reply text and reset the buffer."""
        text = "".join(self.reply_buffer)
        self.reply_buffer = []
        return text
