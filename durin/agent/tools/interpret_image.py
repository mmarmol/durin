"""``interpret_image`` tool — delegate visual interpretation to a vision-capable
auxiliary model.

The primary use case: the user's primary model is text-only (e.g. glm-5.1)
but they have configured ``aux_models.vision`` (e.g. glm-5v-turbo). The
primary can't consume the image bytes directly, but it CAN call this
tool with the image path and a question. The tool routes a one-shot
chat-completion request to the vision-capable aux model and returns its
text response. The primary then reasons over that text.

Why a tool instead of automatic preprocessing
---------------------------------------------

Two reasons make tool-based delegation the right pattern for V1:

1. **Cost control**: not every image attached to a conversation needs
   to be analysed. Auto-preprocessing pays a vision-model call for
   every incidental screenshot; the tool pattern only spends the call
   when the primary decides the image is actually relevant.
2. **Explicit grounding**: the primary frames the question (``what's
   the error in this screenshot?``, ``which colour is the button?``).
   That focused prompt yields better answers than a generic auto-
   description, and the cost is the same.

The trade-off — the primary must know to call the tool — is mitigated
by the description, which spells out the exact pattern.

Registration semantics
----------------------

The tool registers itself **only when** ``ctx.aux_providers["vision"]``
is set. Without an aux vision provider, the tool stays out of the
LLM's tool list entirely so the model isn't tempted to call something
that can't fulfil. This is the config-driven gating the user
specified — no runtime capability detection.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import AuxProviderHandle
from durin.agent.tools.path_utils import resolve_workspace_path
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.telemetry.logger import current_telemetry
from durin.utils.helpers import build_image_content_blocks, detect_image_mime


_MAX_RESPONSE_TOKENS = 2048
_DEFAULT_QUESTION = "Describe this image in detail, including any text visible in it."


@tool_parameters(
    tool_parameters_schema(
        image_path=StringSchema(
            description=(
                "Path to the image file. Either workspace-relative "
                "(e.g. ``screenshots/error.png``) or absolute. The "
                "supported formats are PNG, JPEG, GIF, and WEBP; "
                "format is detected by magic bytes, not extension."
            ),
            min_length=1,
            max_length=2000,
        ),
        question=StringSchema(
            description=(
                "What you want the vision model to tell you about the "
                "image. Be specific — 'transcribe the error message' "
                "or 'which colour is the highlighted button' give "
                "tighter answers than 'describe this'. The default "
                "asks for a detailed description if you really need a "
                "general one."
            ),
            min_length=1,
            max_length=4000,
            nullable=True,
        ),
        required=["image_path"],
    )
)
class InterpretImageTool(Tool):
    """Ask a vision-capable auxiliary model to interpret a local image."""

    _scopes = {"core"}

    def __init__(self, aux: AuxProviderHandle, workspace: str | None) -> None:
        self._aux = aux
        self._workspace = Path(workspace).expanduser() if workspace else None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        aux = (ctx.aux_providers or {}).get("vision")
        assert aux is not None  # guarded by enabled()
        return cls(aux=aux, workspace=getattr(ctx, "workspace", None))

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return bool((getattr(ctx, "aux_providers", None) or {}).get("vision"))

    @property
    def name(self) -> str:
        return "interpret_image"

    @property
    def description(self) -> str:
        return (
            f"Ask the vision-capable auxiliary model ({self._aux.model}) "
            "to interpret a local image. Use when the user has attached "
            "or referenced an image you cannot see directly. Provide the "
            "image path plus a SPECIFIC question — e.g. 'transcribe the "
            "error in the screenshot' or 'list the items in the table'. "
            "Returns the auxiliary model's text response. Do NOT use for "
            "image generation; this is read-only interpretation."
        )

    async def execute(
        self,
        image_path: str | None = None,
        question: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not image_path or not str(image_path).strip():
            return "Error: `image_path` is required."
        prompt = (question or "").strip() or _DEFAULT_QUESTION
        prompt = prompt[:4000]

        try:
            resolved = resolve_workspace_path(
                str(image_path), workspace=self._workspace,
            )
        except PermissionError as e:
            return f"Error: {e}"

        if not resolved.exists():
            return f"Error: image file not found: {image_path}"
        if not resolved.is_file():
            return f"Error: path is not a file: {image_path}"

        try:
            data = resolved.read_bytes()
        except OSError as e:
            return f"Error: could not read image: {e}"

        mime = detect_image_mime(data)
        if mime is None:
            return (
                f"Error: file at {image_path!r} is not a supported image "
                "format. Supported: PNG, JPEG, GIF, WEBP."
            )

        # Build OpenAI-compat content block (image + question text).
        # ``build_image_content_blocks`` already does the base64 dance.
        content_blocks = build_image_content_blocks(
            data, mime, str(resolved), label=prompt,
        )
        messages = [{"role": "user", "content": content_blocks}]

        self._emit("ask_vision.start", {
            "aux_model": self._aux.model,
            "image_bytes": len(data),
            "mime": mime,
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
            self._emit("ask_vision.error", {
                "aux_model": self._aux.model,
                "exception": type(e).__name__,
            })
            return (
                f"Error: vision aux model {self._aux.model!r} failed: "
                f"{type(e).__name__}: {e}"
            )

        content = (response.content or "").strip()
        self._emit("ask_vision.end", {
            "aux_model": self._aux.model,
            "response_chars": len(content),
            "had_content": bool(content),
        })

        if not content:
            return (
                f"Error: vision aux model {self._aux.model!r} returned "
                "no content for this image."
            )
        return (
            f"[via {self._aux.model}]\n{content}"
        )

    @staticmethod
    def _emit(event_type: str, data: dict[str, Any]) -> None:
        logger_obj = current_telemetry()
        if logger_obj is None:
            return
        with suppress(Exception):
            logger_obj.log(event_type, data)
