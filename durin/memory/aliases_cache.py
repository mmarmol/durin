"""Process-wide shared cache for :class:`AliasIndex`.

Implements §2.C of ``docs/archive/36_post_t1_state_and_t2_horizon.md``: three
runtime consumers (``memory_search``, the refine pass,
``EntityAbsorption``) previously each built their own AliasIndex on
first use. Each rebuild parses every entity page in
``memory/entities/<type>/*.md``; harmless but wasteful when the same
``durin agent`` run hits more than one consumer.

This module hands all three the same in-memory instance keyed by
``memory_root``. Mutating callers
(:meth:`AliasIndex.refresh_for`, :meth:`AliasIndex.remove`) update the
shared map in place, so writes by one consumer are immediately visible
to the others — no explicit invalidation needed for the common flow.

:func:`invalidate_alias_index` is provided for paths that bypass the
mutation API (e.g. a user editing a page manually outside the tool,
or a test that wants a fresh build).

Design notes:

- Cache key is the ``memory_root`` path (one entry per workspace).
  Allows different workspaces in the same process (subagents, tests)
  to coexist without cross-contamination.
- A global :class:`threading.Lock` serialises build + invalidate.
  Sub-second build per docstring of :class:`AliasIndex`; lock
  contention is not a practical concern.
- Build failures bubble back to the caller; cache stays empty so the
  next call retries (transient errors like a malformed file added
  mid-build can be fixed and re-tried without restart).
- Tests must call :func:`_clear_all` between cases to avoid carryover.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from durin.memory.aliases_index import AliasIndex

__all__ = [
    "get_shared_alias_index",
    "invalidate_alias_index",
]

logger = logging.getLogger(__name__)

_cache: dict[Path, AliasIndex] = {}
_lock = threading.Lock()


def get_shared_alias_index(memory_root: Path) -> AliasIndex:
    """Return a shared :class:`AliasIndex` for ``memory_root``.

    Builds lazily on first call per workspace; subsequent calls reuse
    the same instance. Always returns a real :class:`AliasIndex` —
    possibly empty if the workspace has no entity pages yet.

    Concurrent calls for the same ``memory_root`` are serialised — only
    one build runs even if multiple consumers race.
    """
    memory_root = Path(memory_root)

    # Fast path: already cached. Read outside the lock since dict
    # lookup is atomic in CPython; only writes need serialisation.
    cached = _cache.get(memory_root)
    if cached is not None:
        return cached

    # Slow path: build under lock so concurrent consumers don't both
    # walk the disk. Recheck inside the lock (double-checked locking).
    with _lock:
        cached = _cache.get(memory_root)
        if cached is not None:
            return cached
        idx = AliasIndex(memory_root)
        # `build()` no-ops if entities/ doesn't exist, so cold
        # workspaces return an empty index — still usable by callers
        # who mutate via refresh_for / add.
        idx.build()
        _cache[memory_root] = idx
        return idx


def invalidate_alias_index(memory_root: Path) -> None:
    """Drop the cached index for ``memory_root``.

    Defensive — the common flow keeps the shared index consistent
    via :meth:`AliasIndex.refresh_for` and :meth:`AliasIndex.remove`,
    which mutate the same instance every consumer holds. Call this
    only when a write path bypasses those (user edited a page out-of-
    band, test wants a fresh build, etc.).

    No-op if there's nothing cached.
    """
    memory_root = Path(memory_root)
    with _lock:
        _cache.pop(memory_root, None)


def _clear_all() -> None:
    """Test-only: forget every cached index.

    Production code should never call this — invalidation is per-
    workspace via :func:`invalidate_alias_index`.
    """
    with _lock:
        _cache.clear()


def _cache_size() -> int:
    """Test-only: number of distinct workspaces currently cached."""
    return len(_cache)
