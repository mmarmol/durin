"""Ask the person in the current chat to approve one request, and wait.

The request is shown by the channel from ``session.metadata["pending_approval"]``:
rich channels (webui, TUI) render a card, and text channels get one serialized
message. The answer never passes through the model. A webui click or a
yes/no reply parsed by the loop resolves a typed ``approval`` waiter.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from durin.agent import pending_answers
from durin.agent.user_payloads import (
    PENDING_APPROVAL_KEY,
    channel_renders_tool_payloads,
    mark_interactions_delivered,
    pending_interaction_items,
)

AskFn = Callable[[dict], Awaitable[str | None]]


@dataclass(frozen=True)
class ChatHandles:
    """What a gated tool needs to ask the person in the current chat: the
    session store, the bus (text channels get the request as a message) and how
    long the turn waits (``agents.defaults.ask_user_answer_timeout_s``).

    The one builder for these handles — every gated tool (skill install,
    skill edit, MCP change, exec) constructs its asker through this class
    rather than re-deriving ``sessions``/``bus``/timeout by hand."""

    sessions: Any = None
    bus: Any = None
    timeout_s: float = 300.0

    @classmethod
    def from_tool_context(cls, ctx: Any) -> "ChatHandles":
        timeout_s = 300.0
        try:
            timeout_s = float(ctx.app_config.agents.defaults.ask_user_answer_timeout_s)
        except AttributeError:
            pass
        return cls(sessions=getattr(ctx, "sessions", None), bus=getattr(ctx, "bus", None),
                   timeout_s=timeout_s)

    def asker(self, request_ctx: Any) -> AskFn | None:
        """The in-chat asker for this turn, or None when nobody can answer."""
        return make_chat_asker(sessions=self.sessions, bus=self.bus,
                               request_ctx=request_ctx, timeout_s=self.timeout_s)


def make_chat_asker(*, sessions: Any, bus: Any, request_ctx: Any,
                    timeout_s: float) -> AskFn | None:
    """An asker bound to this turn's chat, or None when no person can answer."""
    session_key = getattr(request_ctx, "session_key", None)
    if sessions is None or not session_key or not pending_answers.can_block(session_key):
        return None
    channel = getattr(request_ctx, "channel", None)
    chat_id = getattr(request_ctx, "chat_id", None)

    async def ask(record: dict) -> str | None:
        session = sessions.get_or_create(session_key)
        if session.metadata is not None:
            session.metadata[PENDING_APPROVAL_KEY] = {
                "approval_id": record["id"], "kind": record["kind"],
                "summary": record["summary"], "detail": record.get("detail") or {},
            }
            sessions.save(session)
        try:
            if bus is not None and not channel_renders_tool_payloads(channel):
                # Sent on every ask, ignoring the delivered mark: a request
                # asked again after a timeout has the same text, so filtering
                # by "already delivered" would leave the person without it.
                # Marking it delivered afterwards stops the turn-end fallback
                # from sending a second copy.
                items = [i for i in pending_interaction_items(session.metadata)
                         if i[0] == PENDING_APPROVAL_KEY]
                from durin.bus.events import OutboundMessage

                for _key, text in items:
                    with suppress(Exception):
                        await bus.publish_outbound(OutboundMessage(
                            channel=channel, chat_id=chat_id, content=text))
                with suppress(Exception):
                    mark_interactions_delivered(session.metadata, items)
            fut = pending_answers.create(session_key, kind="approval", ref=record["id"])
            try:
                # asyncio.timeout, not wait_for: on Python 3.11 wait_for can
                # swallow a cancellation delivered at the same instant the
                # future resolves, which would leave this waiter stuck past
                # its caller's own cancellation (see loop.py's inbound read).
                async with asyncio.timeout(timeout_s):
                    answer = await fut
            except asyncio.TimeoutError:
                # resolve() can land in the same loop iteration as the
                # timeout's own cancellation; check the future directly
                # instead of treating every TimeoutError as "no answer".
                if fut.done() and not fut.cancelled():
                    verdict = fut.result()
                    return verdict if verdict in ("approve", "reject") else None
                return None
            finally:
                pending_answers.discard(session_key, fut)
            return answer if answer in ("approve", "reject") else None
        finally:
            if session.metadata is not None:
                session.metadata.pop(PENDING_APPROVAL_KEY, None)
                sessions.save(session)

    return ask
