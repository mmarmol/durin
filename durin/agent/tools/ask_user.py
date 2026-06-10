"""``ask_user_question`` tool — explicit clarification mid-turn.

Lets the model pause its work to ask the user a specific question before
proceeding. The pause is implemented as a yield: the tool result tells
the model to present the question as its next assistant message and
stop calling tools. The user's reply arrives as the next inbound message
and naturally continues the conversation with the question in context.

Why this isn't just "have the model type the question"

- **Explicit yield semantics**: the tool result is an unambiguous "stop
  here and wait" signal. Without the tool the model may keep guessing
  parameters or call more tools instead of pausing.
- **Telemetry**: we record where in a session the agent had to clarify,
  which is useful signal for prompt-quality work.
- **UI affordance hook**: ``options`` and a ``pending_question`` marker
  in session metadata let future channels render a structured prompt
  (clickable list, modal, etc.) instead of free-form text.

Design notes (V1)

- We do NOT publish a separate outbound message with the question. That
  would duplicate visibility (the model's own assistant message also
  states the question). Channels that want structured rendering read
  ``session.metadata['pending_question']`` instead, set just before the
  tool returns.
- We do NOT block on the user's reply inside the tool. That would
  require bus interception and a synchronous Future; the V1 ergonomics
  (turn ends, next turn includes context) are good enough and ship now.
  A future V2 can upgrade to a real in-turn pause without changing the
  tool's public schema.
- The tool is allowed in every mode (plan, explore, build) — it never
  touches the workspace, only session metadata.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from durin.agent.user_payloads import (
    channel_renders_tool_payloads,
    serialize_pending_interactions,
)

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import (
    ArraySchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.telemetry.logger import current_telemetry

if TYPE_CHECKING:
    from durin.session.manager import SessionManager


PENDING_QUESTION_KEY = "pending_question"


@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            description=(
                "The exact question to ask the user. Be specific and "
                "actionable — a yes/no or short-answer phrasing is best. "
                "Don't bundle several questions into one call; ask one at "
                "a time and chain calls if you need more than one answer."
            ),
            min_length=1,
            max_length=2000,
        ),
        options=ArraySchema(
            items=StringSchema(min_length=1, max_length=200),
            description=(
                "Optional list of suggested answers (2-6 items). UIs may "
                "render these as a clickable menu. Even if shown, the user "
                "can still answer free-form. Omit when the natural answer "
                "is open-ended or genuinely binary."
            ),
            min_items=2,
            max_items=6,
            nullable=True,
        ),
        required=["question"],
    )
)
class AskUserQuestionTool(Tool, ContextAware):
    """Pause and ask the user a clarifying question."""

    _scopes = {"core"}

    def __init__(
        self,
        sessions: "SessionManager",
        *,
        bus: Any | None = None,
        blocking: bool = True,
        answer_timeout_s: float = 300,
    ) -> None:
        self._sessions = sessions
        self._bus = bus
        self._blocking = blocking
        self._answer_timeout_s = answer_timeout_s
        self._request_ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sessions = getattr(ctx, "sessions", None)
        assert sessions is not None  # guarded by enabled()
        blocking = True
        timeout_s = 300.0
        app_config = getattr(ctx, "app_config", None)
        try:
            defaults = app_config.agents.defaults
            blocking = bool(defaults.ask_user_blocking)
            timeout_s = float(defaults.ask_user_answer_timeout_s)
        except AttributeError:
            pass
        return cls(
            sessions=sessions,
            bus=getattr(ctx, "bus", None),
            blocking=blocking,
            answer_timeout_s=timeout_s,
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "ask_user_question"

    @property
    def description(self) -> str:
        return (
            "Ask the user a specific clarifying question. Use when you "
            "need a decision or piece of information that you cannot "
            "reasonably guess from context — framework choice, ambiguous "
            "file path, scope of change, etc. Do NOT use for every minor "
            "decision; only when the answer materially changes the next "
            "steps. The question is presented to the user by the channel; "
            "when they answer promptly you receive the answer as this "
            "tool's result and continue working, otherwise the turn "
            "yields and their next message is the answer."
        )

    def _session(self) -> Any | None:
        if self._request_ctx is None:
            return None
        key = self._request_ctx.session_key
        if not key:
            return None
        return self._sessions.get_or_create(key)

    async def execute(
        self,
        question: str | None = None,
        options: list[Any] | None = None,
        **kwargs: Any,
    ) -> str:
        if not question or not str(question).strip():
            return "Error: `question` is required and must be non-empty."
        question = str(question).strip()[:2000]

        cleaned_options: list[str] | None = None
        if options:
            try:
                cleaned_options = [
                    str(o).strip()[:200] for o in options if str(o).strip()
                ][:6]
                if len(cleaned_options) < 2:
                    cleaned_options = None
            except Exception:
                cleaned_options = None

        session = self._session()
        question_id = uuid.uuid4().hex[:12]
        if session is not None and session.metadata is not None:
            session.metadata[PENDING_QUESTION_KEY] = {
                "question_id": question_id,
                "question": question,
                "options": cleaned_options or [],
            }
            self._sessions.save(session)

        self._emit("ask_user.question_asked", {
            "question_id": question_id,
            "question_chars": len(question),
            "option_count": len(cleaned_options or []),
        })

        # Blocking V2: wait in-turn for the answer; degrade to the V1 yield
        # contract on timeout, fallback sentinel, or missing session context.
        session_key = self._request_ctx.session_key if self._request_ctx else None
        if self._blocking and session is not None and session_key:
            answer = await self._await_answer(session_key, question_id)
            if answer is not None:
                if session.metadata is not None:
                    session.metadata.pop(PENDING_QUESTION_KEY, None)
                    self._sessions.save(session)
                return (
                    f"The user answered: {answer!r}.\n"
                    "Continue the task using this answer — do not re-ask."
                )

        body = (
            f"Question registered (id={question_id}): {question!r}.\n"
        )
        if cleaned_options:
            body += "Suggested options:\n"
            for opt in cleaned_options:
                body += f"  - {opt}\n"
        body += (
            "\nThe question has been presented to the user by the channel "
            "(interactive panel or message) — do not repeat it verbatim in "
            "your reply. STOP now: do not call more tools, do not start "
            "executing anything. You may add one short line of context. "
            "The user's next message is their answer; you will resume in "
            "the following turn with that answer in context."
        )
        return body

    async def _await_answer(self, session_key: str, question_id: str) -> str | None:
        """Block until the user's in-turn answer; None means fall back to yield."""
        from durin.agent import pending_answers

        await self._publish_dumb_channel_question(session_key)
        fut = pending_answers.create(session_key)
        started = time.monotonic()
        try:
            answer = await asyncio.wait_for(fut, timeout=self._answer_timeout_s)
        except asyncio.TimeoutError:
            self._emit("ask_user.answer_timeout", {
                "question_id": question_id,
                "timeout_s": int(self._answer_timeout_s),
            })
            return None
        finally:
            pending_answers.discard(session_key, fut)
        if answer is pending_answers.FALLBACK or not isinstance(answer, str):
            return None
        self._emit("ask_user.answer_received", {
            "question_id": question_id,
            "wait_ms": int((time.monotonic() - started) * 1000),
        })
        return answer

    async def _publish_dumb_channel_question(self, session_key: str) -> None:
        """Pre-block question delivery for channels without payload rendering.

        Rich channels already rendered the panel from the start tool_event;
        the turn-end fallback serializer never fires while we block, so dumb
        channels need the serialized question published here.
        """
        if self._bus is None or self._request_ctx is None:
            return
        channel = self._request_ctx.channel
        if channel_renders_tool_payloads(channel):
            return
        session = self._session()
        if session is None:
            return
        texts = serialize_pending_interactions(session.metadata)
        for text in texts:
            with suppress(Exception):
                from durin.bus.events import OutboundMessage

                await self._bus.publish_outbound(OutboundMessage(
                    channel=channel,
                    chat_id=self._request_ctx.chat_id,
                    content=text,
                ))

    @staticmethod
    def _emit(event_type: str, data: dict[str, Any]) -> None:
        logger_obj = current_telemetry()
        if logger_obj is None:
            return
        with suppress(Exception):
            logger_obj.log(event_type, data)
