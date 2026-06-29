"""Async message queue for decoupled channel-agent communication."""

import asyncio
import inspect

from durin.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._inbound_authorizer = None

    def set_inbound_authorizer(self, fn) -> None:
        """Install a gate run on every inbound message before it is enqueued.

        fn(msg) returns truthy to allow; falsy to drop (the gate handles any
        side effect like sending a pairing reply). May be sync or async.
        """
        self._inbound_authorizer = fn

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        if self._inbound_authorizer is not None:
            res = self._inbound_authorizer(msg)
            allowed = await res if inspect.isawaitable(res) else res
            if not allowed:
                return
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()
