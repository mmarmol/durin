"""Tests for the ``interpret_audio`` bridge tool.

V1 covers the chat-multimodal path (Gemini, GPT-4o-audio).
Transcription-only models (Whisper) are deferred to a separate
``transcribe_audio`` tool — when the user wires one of those as the
audio aux they will get the dedicated tool's error pointing them
there. We document the V1 limitation explicitly in the tool's error
text and in ``test_provider_error_hints_at_transcribe_audio``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.tools.context import AuxProviderHandle, ToolContext
from durin.agent.tools.interpret_audio import (
    InterpretAudioTool,
    _build_audio_content_blocks,
    _detect_audio_format,
)
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.registry import ToolRegistry
from durin.providers.base import LLMResponse


# Minimal valid WAV header (44 bytes) + empty PCM data. Real audio
# bytes aren't needed; the tool only checks magic bytes for format
# detection and forwards the content to the provider (mocked).
_WAV_HEADER = (
    b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE"
    + b"fmt " + b"\x10\x00\x00\x00\x01\x00\x01\x00"
    + b"\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00"
    + b"data" + b"\x00\x00\x00\x00"
)
_MP3_BYTES = b"ID3\x03\x00\x00\x00" + b"\x00" * 20 + b"\xff\xfb\x90\x00" + b"\x00" * 50
_OGG_BYTES = b"OggS" + b"\x00" * 60


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_detects_wav_via_magic_bytes():
    assert _detect_audio_format(_WAV_HEADER) == "wav"


def test_detects_mp3_via_id3_header():
    assert _detect_audio_format(_MP3_BYTES) == "mp3"


def test_detects_ogg():
    assert _detect_audio_format(_OGG_BYTES) == "ogg"


def test_rejects_non_audio_bytes():
    assert _detect_audio_format(b"not audio at all" + b"\x00" * 60) is None


def test_too_short_input_returns_none():
    assert _detect_audio_format(b"x") is None


# ---------------------------------------------------------------------------
# Content-block construction
# ---------------------------------------------------------------------------


def test_audio_block_is_openai_input_audio_shape():
    blocks = _build_audio_content_blocks(_WAV_HEADER, "wav", "transcribe please")
    assert len(blocks) == 2
    audio = blocks[0]
    assert audio["type"] == "input_audio"
    assert audio["input_audio"]["format"] == "wav"
    assert "data" in audio["input_audio"]
    # Base64 should decode back to the original bytes.
    import base64
    assert base64.b64decode(audio["input_audio"]["data"]) == _WAV_HEADER
    text = blocks[1]
    assert text["type"] == "text" and "transcribe" in text["text"]


# ---------------------------------------------------------------------------
# Tool happy path + error paths
# ---------------------------------------------------------------------------


def _make_tool(tmp_path: Path) -> tuple[InterpretAudioTool, MagicMock]:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LLMResponse(
        content="The audio is silence followed by a single beep.",
        finish_reason="stop",
    ))
    aux = AuxProviderHandle(provider=provider, model="gemini-2.0-flash")
    tool = InterpretAudioTool(aux=aux, workspace=str(tmp_path))
    return tool, provider


@pytest.mark.asyncio
async def test_executes_against_aux_provider(tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(_WAV_HEADER)
    tool, provider = _make_tool(tmp_path)

    out = await tool.execute(audio_path="clip.wav", question="what's here?")

    assert "single beep" in out
    assert "[via gemini-2.0-flash]" in out
    provider.chat.assert_awaited_once()
    blocks = provider.chat.await_args.kwargs["messages"][0]["content"]
    assert any(b.get("type") == "input_audio" for b in blocks)
    audio_block = next(b for b in blocks if b.get("type") == "input_audio")
    assert audio_block["input_audio"]["format"] == "wav"


@pytest.mark.asyncio
async def test_uses_default_question_when_omitted(tmp_path):
    audio = tmp_path / "x.wav"
    audio.write_bytes(_WAV_HEADER)
    tool, provider = _make_tool(tmp_path)

    await tool.execute(audio_path="x.wav")

    text = next(
        b for b in provider.chat.await_args.kwargs["messages"][0]["content"]
        if b.get("type") == "text"
    )["text"]
    assert "transcribe" in text.lower() or "describe" in text.lower()


@pytest.mark.asyncio
async def test_missing_audio_path_errors(tmp_path):
    tool, provider = _make_tool(tmp_path)
    out = await tool.execute()
    assert "Error" in out
    assert "audio_path" in out
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_not_found_returns_error(tmp_path):
    tool, provider = _make_tool(tmp_path)
    out = await tool.execute(audio_path="missing.wav")
    assert "Error" in out
    assert "not found" in out.lower()
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_audio_file_rejected(tmp_path):
    fake = tmp_path / "fake.wav"
    fake.write_text("not really audio at all" + " " * 50)
    tool, provider = _make_tool(tmp_path)

    out = await tool.execute(audio_path="fake.wav", question="?")

    assert "Error" in out
    assert "supported audio format" in out.lower()
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_error_hints_at_transcribe_audio(tmp_path):
    """When the aux model rejects the chat-with-audio shape (likely
    because it's actually a transcription-only model like Whisper),
    the error text should point the user toward the dedicated
    ``transcribe_audio`` tool so they can recover."""
    audio = tmp_path / "x.wav"
    audio.write_bytes(_WAV_HEADER)
    tool, provider = _make_tool(tmp_path)
    provider.chat = AsyncMock(side_effect=RuntimeError("400 invalid input_audio"))

    out = await tool.execute(audio_path="x.wav", question="?")

    assert "Error" in out
    assert "transcribe_audio" in out


@pytest.mark.asyncio
async def test_oversize_file_rejected(tmp_path):
    big = tmp_path / "big.wav"
    big.write_bytes(_WAV_HEADER + b"\x00" * (30 * 1024 * 1024))
    tool, provider = _make_tool(tmp_path)

    out = await tool.execute(audio_path="big.wav", question="?")

    assert "Error" in out
    assert "exceeds" in out.lower() or "limit" in out.lower()
    provider.chat.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tool gating / discovery
# ---------------------------------------------------------------------------


def test_enabled_only_when_audio_aux_present():
    ctx_without = ToolContext(
        config=MagicMock(), workspace="/tmp",
        subagent_manager=MagicMock(), sessions=MagicMock(),
    )
    assert InterpretAudioTool.enabled(ctx_without) is False

    ctx_with = ToolContext(
        config=MagicMock(), workspace="/tmp",
        subagent_manager=MagicMock(), sessions=MagicMock(),
        aux_providers={"audio": AuxProviderHandle(provider=MagicMock(), model="m")},
    )
    assert InterpretAudioTool.enabled(ctx_with) is True


def test_vision_aux_alone_does_not_enable_audio_tool():
    """Configuring vision aux must not accidentally surface the audio
    tool — gating is per-modality."""
    ctx = ToolContext(
        config=MagicMock(), workspace="/tmp",
        subagent_manager=MagicMock(), sessions=MagicMock(),
        aux_providers={"vision": AuxProviderHandle(provider=MagicMock(), model="v")},
    )
    assert InterpretAudioTool.enabled(ctx) is False


def test_discovery_respects_gating():
    ctx_with = ToolContext(
        config=MagicMock(), workspace="/tmp",
        subagent_manager=MagicMock(), sessions=MagicMock(),
        aux_providers={"audio": AuxProviderHandle(provider=MagicMock(), model="m")},
    )
    reg = ToolRegistry()
    loaded = ToolLoader().load(ctx_with, reg, scope="core")
    assert "interpret_audio" in loaded


def test_tool_is_in_plan_mode_allowed_set():
    from durin.agent.agent_mode import PLAN_MODE

    assert PLAN_MODE.is_tool_allowed("interpret_audio")
