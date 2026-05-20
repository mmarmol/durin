"""Memory authorship provenance via ContextVar.

Distinguishes memory writes the agent performed (during background_review,
dream, or any agent-triggered write path) from writes the user authored
by hand. The curator and dream only auto-manage entries marked
``agent_created`` — anything ``user_authored`` is left alone.

The mechanism is a single ContextVar that propagates across ``await``
points and ``asyncio.create_task`` boundaries within the same logical
request, while staying isolated between concurrent tasks.

Usage::

    from durin.memory.provenance import author_scope, current_author

    # Agent-driven write path:
    with author_scope("agent_created"):
        await write_memory_entry(...)

    # User-driven write path: no scope needed; current_author()
    # defaults to "user_authored".
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Literal

__all__ = ["Author", "author_scope", "current_author"]

Author = Literal["user_authored", "agent_created"]

_MEMORY_AUTHOR: ContextVar[Author] = ContextVar(
    "memory_author",
    default="user_authored",
)


def current_author() -> Author:
    """Return the current memory authorship from the ambient context."""
    return _MEMORY_AUTHOR.get()


@contextmanager
def author_scope(author: Author) -> Iterator[None]:
    """Set the memory author within this scope; reset on exit."""
    token = _MEMORY_AUTHOR.set(author)
    try:
        yield
    finally:
        _MEMORY_AUTHOR.reset(token)
