"""``interpret_audio`` tool — delegate audio interpretation to a chat-multimodal
auxiliary model.

Companion to :mod:`durin.agent.tools.interpret_image`. When the primary
model is text-only but the user has configured ``aux_models.audio``
with a chat-multimodal model (Gemini 2.0 with audio, GPT-4o-audio, …),
this tool ships one-shot audio questions to the aux and returns its
text answer.

Scope: chat-multimodal only
----------------------------

This tool uses the OpenAI ``input_audio`` content-block shape, which
chat-multimodal aux models (Gemini, GPT-4o-audio) accept natively. It
does NOT cover **transcription-only** models such as Whisper running on
Ollama or hosted at ``/v1/audio/transcriptions``. Those use a different
endpoint and a verbatim-text contract; they'll be exposed as a separate
``transcribe_audio`` tool once we have a concrete use case for it. If
the user configures a transcription-only model as ``aux_models.audio``
today, this tool will call it as chat and the server will reject the
request — the failure is loud and the next step (add ``transcribe_audio``)
is obvious.

The same gating pattern as ``interpret_image`` applies: the tool only
appears in the LLM's tool list when ``aux_providers["audio"]`` is set.
"""

from __future__ import annotations

import base64
from contextlib import suppress
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import AuxProviderHandle
from durin.agent.tools.path_utils import resolve_workspace_path
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.telemetry.logger import current_telemetry

_MAX_RESPONSE_TOKENS = 2048
_DEFAULT_QUESTION = "Transcribe and describe the audio in detail."
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB — matches typical provider limits


# Audio format detection by magic bytes — extension-independent, like
# the image detector. Returns the value used in the ``format`` field of
# OpenAI-compat ``input_audio`` content blocks.
def _detect_audio_format(data: bytes) -> str | None:
    """Return ``"wav"``/``"mp3"``/``"m4a"``/``"ogg"``/``"flac"``/``"webm"``
    when the magic bytes match a supported audio container, else None."""
    if len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    return None


def _build_audio_content_blocks(
    raw: bytes, audio_format: str, question: str,
) -> list[dict[str, Any]]:
    """OpenAI-compat ``input_audio`` block + text question.

    Mirrors :func:`durin.utils.helpers.build_image_content_blocks` —
    base64-encode the audio bytes, attach a ``format`` field for the
    server's decoder, and put the user's question right after.
    """
    b64 = base64.b64encode(raw).decode()
    return [
        {
            "type": "input_audio",
            "input_audio": {"data": b64, "format": audio_format},
        },
        {"type": "text", "text": question},
    ]


@tool_parameters(
    tool_parameters_schema(
        audio_path=StringSchema(
            description=(
                "Path to the audio file. Either workspace-relative or "
                "absolute. Supported formats: WAV, MP3, M4A, OGG, "
                "FLAC, WebM — detected by magic bytes, not extension."
            ),
            min_length=1,
            max_length=2000,
        ),
        question=StringSchema(
            description=(
                "What you want the audio model to tell you. Examples: "
                "'transcribe this verbatim', 'summarise what the "
                "speaker says', 'identify the language'. The default "
                "asks for a detailed transcript-plus-description."
            ),
            min_length=1,
            max_length=4000,
            nullable=True,
        ),
        required=["audio_path"],
    )
)
class InterpretAudioTool(Tool):
    """Ask a chat-multimodal auxiliary model to interpret a local audio file."""

    _scopes = {"core"}

    def __init__(self, aux: AuxProviderHandle, workspace: str | None) -> None:
        self._aux = aux
        self._workspace = Path(workspace).expanduser() if workspace else None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        aux = (ctx.aux_providers or {}).get("audio")
        assert aux is not None  # guarded by enabled()
        return cls(aux=aux, workspace=getattr(ctx, "workspace", None))

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return bool((getattr(ctx, "aux_providers", None) or {}).get("audio"))

    @property
    def name(self) -> str:
        return "interpret_audio"

    @property
    def description(self) -> str:
        return (
            f"Ask the audio-capable auxiliary model ({self._aux.model}) "
            "to interpret a local audio file. Use when the user has "
            "attached or referenced an audio clip you cannot process "
            "directly. Provide the audio path plus a SPECIFIC question — "
            "'transcribe this verbatim', 'summarise the speaker's "
            "argument', 'identify the language'. Supports WAV, MP3, "
            "M4A, OGG, FLAC, WebM. Returns the auxiliary model's text "
            "response. Do NOT use for text-to-speech generation."
        )

    async def execute(
        self,
        audio_path: str | None = None,
        question: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not audio_path or not str(audio_path).strip():
            return "Error: `audio_path` is required."
        prompt = (question or "").strip() or _DEFAULT_QUESTION
        prompt = prompt[:4000]

        try:
            resolved = resolve_workspace_path(
                str(audio_path), workspace=self._workspace,
            )
        except PermissionError as e:
            return f"Error: {e}"

        if not resolved.exists():
            return f"Error: audio file not found: {audio_path}"
        if not resolved.is_file():
            return f"Error: path is not a file: {audio_path}"

        try:
            data = resolved.read_bytes()
        except OSError as e:
            return f"Error: could not read audio file: {e}"

        if len(data) > _MAX_AUDIO_BYTES:
            return (
                f"Error: audio file is {len(data) // (1024*1024)} MB, "
                f"exceeds the {_MAX_AUDIO_BYTES // (1024*1024)} MB limit "
                "supported by most aux models."
            )

        audio_format = _detect_audio_format(data)
        if audio_format is None:
            return (
                f"Error: file at {audio_path!r} is not a supported audio "
                "format. Supported: WAV, MP3, M4A, OGG, FLAC, WebM."
            )

        content_blocks = _build_audio_content_blocks(data, audio_format, prompt)
        messages = [{"role": "user", "content": content_blocks}]

        self._emit("ask_audio.start", {
            "aux_model": self._aux.model,
            "audio_bytes": len(data),
            "format": audio_format,
            "question_chars": len(prompt),
        })

        try:
            response = await self._aux.provider.chat(
                messages=messages,
                model=self._aux.model,
                max_tokens=_MAX_RESPONSE_TOKENS,
                temperature=0.1,
            )
        except Exception as e:
            self._emit("ask_audio.error", {
                "aux_model": self._aux.model,
                "exception": type(e).__name__,
            })
            return (
                f"Error: audio aux model {self._aux.model!r} failed: "
                f"{type(e).__name__}: {e}. (If you configured a "
                "transcription-only model like Whisper, use the "
                "dedicated transcribe_audio tool instead — interpret_audio "
                "speaks the chat-multimodal protocol.)"
            )

        content = (response.content or "").strip()
        self._emit("ask_audio.end", {
            "aux_model": self._aux.model,
            "response_chars": len(content),
            "had_content": bool(content),
        })

        if not content:
            return (
                f"Error: audio aux model {self._aux.model!r} returned "
                "no content for this audio file."
            )
        return f"[via {self._aux.model}]\n{content}"

    @staticmethod
    def _emit(event_type: str, data: dict[str, Any]) -> None:
        logger_obj = current_telemetry()
        if logger_obj is None:
            return
        with suppress(Exception):
            logger_obj.log(event_type, data)
