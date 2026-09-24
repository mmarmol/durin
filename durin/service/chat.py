"""ChatService — converse with durin over HTTP in webui conversations.

A message sent here enters the same path as a webui message: the websocket
channel validates it and hands it to the agent on the bus, so the turn runs
server-side, detached from the request. Watching the turn is the SSE route the
HTTP front door mounts; stopping it cancels the turn in the agent loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ConfigDict, Field

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ForbiddenError,
    Result,
    ServiceModel,
    UnavailableError,
    ValidationFailedError,
)

WEBUI_KEY_PREFIX = "websocket:"


def webui_chat_id(key: str) -> str:
    """Return the chat id of a webui conversation key, or raise 422.

    Only webui conversations accept messages from the API: another channel's
    session belongs to an outside conversation (a Slack thread, a Telegram
    chat) that durin must not be made to speak into from here."""
    from durin.channels.websocket import _is_valid_chat_id

    if not key.startswith(WEBUI_KEY_PREFIX):
        raise ValidationFailedError(
            "only webui conversations (websocket:<id>) accept messages",
            details={"key": key},
        )
    chat_id = key[len(WEBUI_KEY_PREFIX):]
    if not _is_valid_chat_id(chat_id):
        raise ValidationFailedError("invalid conversation id", details={"key": key})
    return chat_id


class ChatMediaItem(ServiceModel):
    model_config = ConfigDict(extra="forbid")

    data_url: str
    name: str | None = None


class ChatSendCommand(Command):
    key: str
    content: str = ""
    media: list[ChatMediaItem] | None = None
    steer: bool = False
    client_msg_id: str | None = Field(default=None, max_length=64)


class ChatSendResult(Result):
    key: str
    client_msg_id: str | None = None


class ChatStopCommand(Command):
    key: str


class ChatStopResult(Result):
    stopped: int


class ChatService:
    """Send messages into webui conversations and stop their turns."""

    def __init__(
        self,
        channel_resolver: Callable[[], Any] | None = None,
        stop_turn: Callable[[str], Awaitable[int]] | None = None,
        turn_key: Callable[[str], str] | None = None,
    ) -> None:
        self._channel_resolver = channel_resolver
        self._stop_turn = stop_turn
        self._turn_key = turn_key

    def _channel(self) -> Any:
        channel = self._channel_resolver() if self._channel_resolver else None
        if channel is None:
            raise UnavailableError("the webui chat channel is not running")
        return channel

    @staticmethod
    def _sender(principal: Principal) -> str:
        return f"api:{principal.subject}"

    @route(
        "POST",
        "/api/v1/sessions/{key}/messages",
        scope=Scope.CHAT_WRITE.value,
        request_model=ChatSendCommand,
        response_model=ChatSendResult,
        summary="Send a message to a webui conversation; the turn runs server-side",
        status_code=202,
    )
    async def send(self, cmd: ChatSendCommand, principal: Principal) -> ChatSendResult:
        chat_id, media_paths = self.check(cmd, principal)
        await self.deliver(cmd, principal, chat_id, media_paths)
        return ChatSendResult(key=cmd.key, client_msg_id=cmd.client_msg_id)

    def check(self, cmd: ChatSendCommand, principal: Principal) -> tuple[str, list[str]]:
        """Everything that can refuse a message, before anything is published:
        scope, conversation key, sender allowlist, content and media. Returns
        the chat id and the saved media paths."""
        principal.require(Scope.CHAT_WRITE)
        chat_id = webui_chat_id(cmd.key)
        channel = self._channel()
        sender_id = self._sender(principal)
        # The bus ingress gate would drop a disallowed sender after we already
        # answered 202; refuse up front so the caller learns why.
        if not channel.is_allowed(sender_id):
            raise ForbiddenError(
                "this token's sender is not in channels.websocket.allowFrom",
                details={"sender_id": sender_id},
            )
        raw_media = [m.model_dump() for m in cmd.media] if cmd.media is not None else None
        return chat_id, channel.validate_chat_message(chat_id, cmd.content, raw_media)

    async def deliver(
        self, cmd: ChatSendCommand, principal: Principal, chat_id: str, media_paths: list[str],
    ) -> None:
        """Hand a checked message to the agent (see ``check``)."""
        await self._channel().publish_chat_message(
            sender_id=self._sender(principal),
            chat_id=chat_id,
            content=cmd.content,
            media_paths=media_paths,
            webui=True,
            steer=cmd.steer,
            client_msg_id=cmd.client_msg_id,
            origin="api",
        )

    @route(
        "POST",
        "/api/v1/sessions/{key}/stop",
        scope=Scope.CHAT_WRITE.value,
        request_model=ChatStopCommand,
        response_model=ChatStopResult,
        summary="Stop the running turn of a webui conversation",
    )
    async def stop(self, cmd: ChatStopCommand, principal: Principal) -> ChatStopResult:
        principal.require(Scope.CHAT_WRITE)
        webui_chat_id(cmd.key)
        if self._stop_turn is None or self._turn_key is None:
            raise UnavailableError("the agent loop is not available")
        return ChatStopResult(stopped=await self._stop_turn(self._turn_key(cmd.key)))
