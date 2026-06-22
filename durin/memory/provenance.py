"""Memory authorship provenance via ContextVar.

Distinguishes memory writes the agent performed (during background_review,
dream, or any agent-triggered write path) from writes the user authored
by hand. The curator and dream only auto-manage entries marked
``agent_created`` — anything ``user_authored`` is left alone.

The mechanism is a single ContextVar that propagates across ``await``
points and ``asyncio.create_task`` boundaries within the same logical
request, while staying isolated between concurrent tasks.

**No default.** Every memory write must declare its author explicitly
by opening an :func:`author_scope`. Calling :func:`current_author`
outside an active scope raises :class:`MissingAuthorScopeError` — a
loud failure beats a silent mismarking. Rationale: the implicit
default makes "forgot to wrap" indistinguishable from "intentional
default", which has bitten benchmarks + tests already (see
``docs/internals/memory/01_data_and_entities.md`` §4.6.1).

Usage::

    from durin.memory.provenance import author_scope

    # Agent-driven write path (every memory_* tool wraps itself):
    with author_scope("agent_created"):
        store_memory(workspace, ...)

    # User-driven write path (e.g. /remember slash command):
    with author_scope("user_authored"):
        store_memory(workspace, ...)
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Literal, Optional

__all__ = [
    "Author",
    "MissingAuthorScopeError",
    "author_scope",
    "current_author",
]

Author = Literal["user_authored", "agent_created"]


class MissingAuthorScopeError(RuntimeError):
    """Raised when a memory write happens without an active ``author_scope``.

    Indicates a caller forgot to declare authorship. Fix the caller:
    wrap the write in ``author_scope("agent_created")`` or
    ``author_scope("user_authored")`` per the intent of that path.
    """


# `None` sentinel = no active scope. The ContextVar default is `None`
# precisely because storing a real string here would let callers
# silently get the wrong author on missing-scope bugs.
_MEMORY_AUTHOR: ContextVar[Optional[Author]] = ContextVar(
    "memory_author",
    default=None,
)


def current_author() -> Author:
    """Return the current memory authorship from the ambient context.

    Raises :class:`MissingAuthorScopeError` when no scope is active.
    Callers must wrap their memory write in :func:`author_scope`.
    """
    value = _MEMORY_AUTHOR.get()
    if value is None:
        raise MissingAuthorScopeError(
            "memory write attempted without an active author_scope. "
            "Wrap the write in author_scope('agent_created') or "
            "author_scope('user_authored')."
        )
    return value


@contextmanager
def author_scope(author: Author) -> Iterator[None]:
    """Set the memory author within this scope; reset on exit."""
    token = _MEMORY_AUTHOR.set(author)
    try:
        yield
    finally:
        _MEMORY_AUTHOR.reset(token)
