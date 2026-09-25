"""A turn's concurrency slots, given back while it waits on a person.

A turn holds one interactive-lane slot and one ceiling slot while it runs. A
turn blocked on a person (an approval card, a blocking question) can wait
minutes, and holding its slots meanwhile lets a few unanswered requests stall
every other chat. So while such a wait lasts, the turn gives its slots back,
and it takes them again before it continues. Its session lock stays held: the
turn still owns its session.

The slots a turn holds are one ``TurnSlots`` object with a held flag per
slot. Every release and every acquire goes through those flags, so a
cancellation landing anywhere (during the wait, or while queued to take a
slot back) leaves exactly the held slots to release: none released twice,
none leaked.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from typing import Any, AsyncIterator, Callable

__all__ = ["TurnSlots", "bind", "released_while_waiting", "unbind"]


class TurnSlots:
    """The lane and ceiling slots one turn holds, as an async context manager.

    ``async with slots:`` takes them in order (the lane, then the ceiling) and
    gives back on exit only those still held. ``gates`` are
    ``ResizableSemaphore``s, driven through their own ``__aenter__`` /
    ``__aexit__`` so their live counters stay right.
    """

    def __init__(self, *gates: Any, session_key: str | None,
                 on_change: Callable[[], None] | None = None) -> None:
        self._gates = gates
        self._held = [False] * len(gates)
        self.session_key = session_key
        self._on_change = on_change
        # Set once the turn is over: a task the turn started that outlives it
        # still sees this object in its context, and must never take a slot
        # nobody would give back.
        self.closed = False

    @property
    def held(self) -> tuple[bool, ...]:
        return tuple(self._held)

    async def acquire(self) -> None:
        """Take every slot not held yet, in order. A cancellation while queued
        for one leaves the flags saying exactly which are held."""
        if self.closed:
            return
        for i, gate in enumerate(self._gates):
            if not self._held[i]:
                await gate.__aenter__()
                # No suspension point between the gate granting the slot and
                # this line, so a cancellation cannot fall between them.
                self._held[i] = True

    async def release(self) -> None:
        """Give back every held slot, in reverse order."""
        for i in reversed(range(len(self._gates))):
            if self._held[i]:
                # Cleared first: a slot is never given back twice.
                self._held[i] = False
                await self._gates[i].__aexit__(None, None, None)

    def changed(self) -> None:
        """Tell whoever reports the lanes that this turn's slots moved."""
        if self._on_change is not None:
            self._on_change()

    async def __aenter__(self) -> "TurnSlots":
        try:
            await self.acquire()
        except BaseException:
            # ``async with`` never calls __aexit__ when __aenter__ raises, so
            # a slot taken before the failure is given back here.
            await self.release()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True
        await self.release()


_CURRENT: ContextVar[TurnSlots | None] = ContextVar("turn_slots", default=None)


def bind(slots: TurnSlots) -> Token:
    """Make *slots* the current turn's slots, for this task and the tasks it
    starts (they copy its context)."""
    return _CURRENT.set(slots)


def unbind(token: Token) -> None:
    _CURRENT.reset(token)


@asynccontextmanager
async def released_while_waiting(session_key: str | None) -> AsyncIterator[None]:
    """Give the current turn's slots back for the duration of a wait on a
    person in *session_key*, and take them again before the turn continues.

    A no-op outside a turn the loop dispatched (the CLI, a test), in a task
    working for another session (a sub-agent, a background run started by the
    turn, which copied its context), and once the turn is over.

    A cancelled wait does not take the slots back: the turn is ending, and
    waiting for a free slot would hold up ``/stop`` or a shutdown. The turn
    releases on exit only what it holds.
    """
    slots = _CURRENT.get()
    if slots is None or slots.closed or slots.session_key != session_key:
        yield
        return
    await slots.release()
    slots.changed()
    try:
        yield
    except asyncio.CancelledError:
        raise
    except BaseException:
        await slots.acquire()
        slots.changed()
        raise
    await slots.acquire()
    slots.changed()
