"""Process-wide shared cache for :class:`AliasIndex`.

Three runtime consumers (``memory_search``, the refine pass,
``EntityAbsorption``) previously each built their own AliasIndex on
first use. Each rebuild parses every entity page in
``memory/entities/<type>/*.md``; harmless but wasteful when the same
``durin agent`` run hits more than one consumer.

This module hands all three the same in-memory instance keyed by
``memory_root``. Mutating callers
(:meth:`AliasIndex.refresh_for`, :meth:`AliasIndex.remove`) update the
shared map in place, so writes by one consumer are immediately visible
to the others — no explicit invalidation needed for the common flow.

Writes from another process (a dream worker) never reach this map, so the
gateway calls :func:`refresh_alias_index_in_background` after each dream
worker exits, and once at startup. The rebuild walks every page — seconds
on a workspace with thousands of them — on a daemon thread, and the
instance swaps in the new map atomically at the end (see
:meth:`AliasIndex.build`). Until then every caller keeps getting the
previous map without waiting. Only a caller that finds no index at all
waits, and only for the build already in flight.

:func:`invalidate_alias_index` is provided for paths that bypass the
mutation API (e.g. a user editing a page manually outside the tool,
or a test that wants a fresh build).

Design notes:

- Cache key is the ``memory_root`` path (one entry per workspace).
  Allows different workspaces in the same process (subagents, tests)
  to coexist without cross-contamination.
- A global :class:`threading.Lock` serialises the first build of a
  workspace and invalidation, so concurrent first callers share one
  build. Rebuilds of an already-cached index run outside that lock:
  readers never block on them.
- The index lives in this process's memory only; nothing here reads,
  modifies and writes shared state on disk.
- Build failures bubble back to the caller; cache stays empty so the
  next call retries (transient errors like a malformed file added
  mid-build can be fixed and re-tried without restart). A failed
  background rebuild is logged and the previous map keeps serving.
- Tests must call :func:`_clear_all` between cases to avoid carryover.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from loguru import logger

from durin.memory.aliases_index import AliasIndex

__all__ = [
    "alias_index_for_writes",
    "get_shared_alias_index",
    "invalidate_alias_index",
    "refresh_alias_index_in_background",
]

_cache: dict[Path, AliasIndex] = {}
# Instances whose first build is running. Writers apply their changes to
# these too, and the build replays them at its swap instead of losing a
# write whose page the walk had already read.
_building: dict[Path, AliasIndex] = {}
_lock = threading.Lock()

# Background rebuild bookkeeping, guarded by `_refresh_lock`: workspaces with
# a rebuild thread running, and those that asked for another rebuild while
# one was running (its walk may have listed the pages before the new writes).
_refresh_lock = threading.Lock()
_refreshing: set[Path] = set()
_refresh_again: set[Path] = set()


def get_shared_alias_index(memory_root: Path) -> AliasIndex:
    """Return a shared :class:`AliasIndex` for ``memory_root``.

    Builds lazily on first call per workspace; subsequent calls reuse
    the same instance. Always returns a real :class:`AliasIndex` —
    possibly empty if the workspace has no entity pages yet.

    Concurrent calls for the same ``memory_root`` are serialised — only
    one build runs even if multiple consumers race, including a first
    build started by :func:`refresh_alias_index_in_background`.
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
        _building[memory_root] = idx
        try:
            # `build()` no-ops if entities/ doesn't exist, so cold
            # workspaces return an empty index — still usable by callers
            # who mutate via refresh_for / add.
            idx.build()
            # Published before leaving `_building`, so a writer checking
            # `_building` then `_cache` always finds it in one of them.
            _cache[memory_root] = idx
        finally:
            _building.pop(memory_root, None)
        return idx


def alias_index_for_writes(memory_root: Path) -> AliasIndex | None:
    """The index a write should update: the cached one, or the one whose
    first build is running. ``None`` when neither exists — the next build
    reads the write from disk. Never starts a build."""
    memory_root = Path(memory_root)
    building = _building.get(memory_root)
    if building is not None:
        return building
    return _cache.get(memory_root)


def refresh_alias_index_in_background(memory_root: Path) -> threading.Thread | None:
    """Rebuild the shared index for ``memory_root`` from disk on a daemon
    thread.

    With an index cached, callers keep getting it — the old map — until the
    rebuilt map swaps in. With none cached, this is the first build: callers
    arriving meanwhile wait for it instead of starting their own. A request
    while a rebuild is running folds into one more pass after it, so writes
    landing mid-walk are read too; a request while a caller's first build is
    running waits for that build, then runs one more pass.

    Returns the started thread, or ``None`` when the running rebuild took
    the request.
    """
    memory_root = Path(memory_root)
    with _refresh_lock:
        if memory_root in _refreshing:
            _refresh_again.add(memory_root)
            return None
        _refreshing.add(memory_root)
    thread = threading.Thread(
        target=_refresh_until_settled, args=(memory_root,),
        daemon=True, name="alias-index-refresh",
    )
    try:
        thread.start()
    except BaseException:
        with _refresh_lock:
            _refreshing.discard(memory_root)
        raise
    return thread


def _refresh_until_settled(memory_root: Path) -> None:
    while True:
        t0 = time.perf_counter()
        try:
            # `_building` before `_cache`: a first build is published to the
            # cache before it leaves `_building`, so it is seen in one of them.
            in_flight = _building.get(memory_root)
            cached = _cache.get(memory_root)
            if cached is None:
                # Builds the index, or waits for the first build in flight.
                idx = get_shared_alias_index(memory_root)
                if idx is in_flight:
                    # That build started before this request, so its walk
                    # may have listed the pages before the writes the
                    # request is for: one more pass reads them.
                    idx.build()
            else:
                cached.build()
                idx = cached
            logger.info(
                "alias index rebuilt in background ({}ms, {} aliases, {})",
                int((time.perf_counter() - t0) * 1000), idx.size(), memory_root,
            )
        except Exception:
            logger.exception("alias index background rebuild failed ({})", memory_root)
        with _refresh_lock:
            if memory_root not in _refresh_again:
                _refreshing.discard(memory_root)
                return
            _refresh_again.discard(memory_root)


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
