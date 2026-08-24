"""ChannelPostService — post through a live channel, on durin's record.

Workflow script nodes run as subprocesses, so they cannot reach the in-process
channel manager. With no door available they post to the platform API directly,
which is how the ticket pipeline's Slack posts became invisible to durin: the
conversation existed on Slack and nowhere else. A session is only ever created
by the loop consuming an inbound message, so an investigation nobody replied to
left no chat behind at all — and when somebody did reply, the resulting session
started blank, with none of the work the pipeline had already posted.

Recording is therefore the point of this route, not a side effect: an outbound
send alone would still leave no session. The key it records under is the one
the channel itself derives for that conversation, so a later human reply lands
in the same session and continues it instead of opening a second one.

This does not carry an automation's *status* into a counterpart's thread —
that stays routed by ``durin.automations.outcome.route``, which deliberately
refuses to report internal status to the external party a channel origin
identifies. What travels here is the workflow's own prose, which was already
being posted anyway.
"""

from __future__ import annotations

from typing import Any

from durin.bus.events import OutboundMessage
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Result


class ChannelPostCommand(Command):
    #: Channel name as registered in the running gateway, e.g. "slack".
    channel: str
    #: Conversation id within that channel (a Slack channel id, a chat id, …).
    chat_id: str
    text: str
    #: Thread within the conversation, when the channel threads. Slack is the
    #: only one that does today; elsewhere leave it unset.
    thread_id: str | None = None
    #: Persist the post to the session transcript. On by default — a post that
    #: is not recorded is invisible to durin, which is the bug this route exists
    #: to close. Turn it off only for genuinely throwaway chatter.
    record: bool = True


class ChannelPostResult(Result):
    ok: bool
    #: The session the post was recorded under, or None when record was false.
    session_key: str | None = None
    error: str | None = None


class ChannelPostService:
    """HTTP surface for posting into a conversation as durin."""

    def __init__(
        self,
        *,
        channel_manager: Any | None = None,
        session_manager: Any | None = None,
    ) -> None:
        self._channel_manager = channel_manager
        self._session_manager = session_manager

    @staticmethod
    def session_key_for(channel: str, chat_id: str, thread_id: str | None) -> str:
        """The key the channel itself would derive for this conversation.

        Matching it is what makes a later human reply continue this session
        rather than open a parallel one: the loop keys an inbound message by
        ``channel:chat_id``, and a threading channel appends the thread id
        (see ``SlackChannel._handle_message``).
        """
        base = f"{channel}:{chat_id}"
        return f"{base}:{thread_id}" if thread_id else base

    @staticmethod
    def _thread_metadata(channel: str, thread_id: str | None) -> dict[str, Any]:
        """Per-channel threading hint for the outbound message.

        Threading is a per-channel convention, not a shared one, so this maps
        rather than generalises. Slack is the only channel that threads today;
        a second one adds its own branch here.
        """
        if thread_id and channel == "slack":
            return {"slack": {"thread_ts": thread_id}}
        return {}

    @route(
        "POST",
        "/api/v1/channels/post",
        scope=Scope.CHANNELS_WRITE.value,
        request_model=ChannelPostCommand,
        response_model=ChannelPostResult,
        summary="Post a message through a running channel and record it in the session",
    )
    async def post(self, cmd: ChannelPostCommand, principal: Principal) -> ChannelPostResult:
        principal.require(Scope.CHANNELS_WRITE)
        if self._channel_manager is None:
            return ChannelPostResult(ok=False, error="channel_manager not available")
        channel = self._channel_manager.get_channel(cmd.channel)
        if channel is None:
            return ChannelPostResult(
                ok=False, error=f"channel {cmd.channel!r} is not running"
            )

        try:
            await channel.send(OutboundMessage(
                channel=cmd.channel,
                chat_id=cmd.chat_id,
                content=cmd.text,
                metadata=self._thread_metadata(cmd.channel, cmd.thread_id),
            ))
        except Exception as e:  # noqa: BLE001 — the caller decides how to react
            return ChannelPostResult(ok=False, error=str(e))

        session_key = self.session_key_for(cmd.channel, cmd.chat_id, cmd.thread_id)
        if not cmd.record:
            return ChannelPostResult(ok=True)
        if self._session_manager is None:
            return ChannelPostResult(
                ok=True, error="session_manager unavailable — post was not recorded"
            )
        # Written straight to the transcript rather than published as an
        # inbound event: an inbound would run a turn, and durin would answer
        # its own post. The record is the whole intent here.
        session = self._session_manager.get_or_create(session_key)
        session.add_message("assistant", cmd.text)
        self._session_manager.save(session)
        return ChannelPostResult(ok=True, session_key=session_key)
