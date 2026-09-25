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

from durin.agent import approval, pending_answers
from durin.agent.turn_slots import released_while_waiting
from durin.agent.user_payloads import (
    PENDING_APPROVAL_KEY,
    channel_renders_tool_payloads,
    mark_interactions_delivered,
    pending_interaction_items,
    push_session_state,
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
    """An asker bound to this turn's chat, or None when no person can answer.

    None too in a turn with input from an API token: a program took part in
    it, so its privileged requests are not put to the chat as the person's
    decision. Without an asker they take the no-person path (filed as
    pending, or refused for exec)."""
    session_key = getattr(request_ctx, "session_key", None)
    if sessions is None or not session_key or not pending_answers.can_block(session_key):
        return None
    if approval.turn_has_api_input():
        return None
    channel = getattr(request_ctx, "channel", None)
    chat_id = getattr(request_ctx, "chat_id", None)

    async def push_card_state(session: Any) -> None:
        """Rich channels draw the approval card from session state, which
        they otherwise receive only at turn end. Push the snapshot now so the
        card appears while the turn waits, and clears once it is answered."""
        await push_session_state(bus, channel, chat_id, session.metadata)

    async def ask(record: dict) -> str | None:
        session = sessions.get_or_create(session_key)
        if session.metadata is not None:
            session.metadata[PENDING_APPROVAL_KEY] = {
                "approval_id": record["id"], "kind": record["kind"],
                "summary": record["summary"], "detail": record.get("detail") or {},
            }
            sessions.save(session)
        try:
            await push_card_state(session)
            if bus is not None and not channel_renders_tool_payloads(channel):
                # Sent on every ask, ignoring the delivered mark: a request
                # asked again after a timeout has the same text, so filtering
                # by "already delivered" would leave the person without it.
                # Marking it delivered afterwards stops the turn-end fallback
                # from sending a second copy.
                items = [i for i in pending_interaction_items(session.metadata)
                         if i[0] == PENDING_APPROVAL_KEY]
                from durin.bus.events import OUTBOUND_META_ASKS_PERSON, OutboundMessage

                # Carry the turn's own metadata (thread_ts, forum topic id, …)
                # so the request lands in the conversation the turn belongs
                # to rather than at the surface's top level, and flag it so
                # Slack notifies instead of silently editing a status line.
                turn_metadata = getattr(request_ctx, "metadata", None) or {}
                for _key, text in items:
                    with suppress(Exception):
                        await bus.publish_outbound(OutboundMessage(
                            channel=channel, chat_id=chat_id, content=text,
                            metadata={**dict(turn_metadata), OUTBOUND_META_ASKS_PERSON: True}))
                with suppress(Exception):
                    mark_interactions_delivered(session.metadata, items)
            fut = pending_answers.create(session_key, kind="approval", ref=record["id"])
            try:
                # The turn gives its concurrency slots back while the person
                # decides, so other chats keep running, and takes them again
                # before it goes on.
                async with released_while_waiting(session_key):
                    try:
                        # asyncio.timeout, not wait_for: on Python 3.11
                        # wait_for can swallow a cancellation delivered at the
                        # same instant the future resolves, which would leave
                        # this waiter stuck past its caller's own cancellation
                        # (see loop.py's inbound read).
                        async with asyncio.timeout(timeout_s):
                            answer = await fut
                    except asyncio.TimeoutError:
                        # resolve() can land in the same loop iteration as the
                        # timeout's own cancellation; check the future directly
                        # instead of treating every TimeoutError as "no answer".
                        answer = fut.result() if fut.done() and not fut.cancelled() else None
            finally:
                pending_answers.discard(session_key, fut)
            return answer if answer in ("approve", "reject") else None
        finally:
            if session.metadata is not None:
                session.metadata.pop(PENDING_APPROVAL_KEY, None)
                sessions.save(session)
            await push_card_state(session)

    return ask
