"""Frozen per-session eager memory surface: the pinned block + hot layer snapshot.

The pinned memory block and the hot layer are normally rebuilt from disk on
every prompt build. Any entity write during a session changes a page's
``updated_at``, which reorders the hot layer's canonical block (and can
change the pinned block), invalidating the upstream provider's cached
prompt prefix on the very next call. Freezing the rendering at the first
prompt build of a session and reusing it verbatim for the rest of the
session turns that per-turn cache invalidation into at most one per
session.

``EagerSnapshot`` is that frozen rendering. It lives in
``session.metadata[SNAPSHOT_KEY]`` as the plain dict produced by
``to_metadata()`` — session metadata is persisted as JSON via the session
sidecar, and the dataclass itself (with its ``frozenset`` field) is not
directly serialisable. ``SNAPSHOT_KEY`` is registered as derived (sidecar)
metadata: it rides in the session's ``.meta.json`` sidecar, never in the
``.jsonl`` identity line, because it is a rendering artifact reconstructible
from the memory store rather than session content.

This module defines only the snapshot type and its staleness rule.
Building a snapshot during a prompt build and consuming it there (including
the in-context dedup reading frozen refs instead of the live hot layer) is
wired in separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# session.metadata key for the frozen snapshot. Registered in
# SessionManager._DERIVED_METADATA_KEYS so it is split into the
# `.meta.json` sidecar at save time and merged back on load, the same way
# `_last_summary` is.
SNAPSHOT_KEY = "_eager_surface"


@dataclass(frozen=True)
class EagerSnapshot:
    """A frozen rendering of the pinned memory block and the hot layer.

    ``refs`` is the set of entity refs the pinned block resolved (the same
    shape the pinned-block builder returns today), carried alongside the
    text so a later in-context dedup can judge containment against what the
    model was actually shown, not against a live hot layer that may have
    moved on. ``turn`` is the message-count position the snapshot was taken
    at; ``frozen_at`` is an ISO-8601 UTC timestamp consumed by
    ``snapshot_is_stale``.
    """

    pinned: str
    hot: str
    refs: frozenset[str]
    turn: int
    frozen_at: str

    def to_metadata(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict for ``session.metadata[SNAPSHOT_KEY]``."""
        return {
            "pinned": self.pinned,
            "hot": self.hot,
            "refs": sorted(self.refs),
            "turn": self.turn,
            "frozen_at": self.frozen_at,
        }

    @classmethod
    def from_metadata(cls, data: Any) -> "EagerSnapshot | None":
        """Reconstruct from ``to_metadata`` output.

        Returns ``None`` on any shape error — a missing sidecar, a
        hand-edited file, a future format — rather than raising, so callers
        fall back to a live render instead of the prompt build crashing.
        """
        if not isinstance(data, dict):
            return None
        pinned = data.get("pinned")
        hot = data.get("hot")
        refs = data.get("refs")
        turn = data.get("turn")
        frozen_at = data.get("frozen_at")
        if not isinstance(pinned, str) or not isinstance(hot, str):
            return None
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            return None
        if not isinstance(turn, int) or not isinstance(frozen_at, str):
            return None
        try:
            datetime.fromisoformat(frozen_at)
        except (ValueError, TypeError):
            return None
        return cls(pinned=pinned, hot=hot, refs=frozenset(refs), turn=turn, frozen_at=frozen_at)


def snapshot_is_stale(snap: EagerSnapshot, *, refresh_after_min: int, now: datetime | None = None) -> bool:
    """True once *snap* is older than ``refresh_after_min`` minutes.

    ``refresh_after_min <= 0`` means "session boundaries only" — the
    snapshot never goes stale by age alone, matching
    ``MemoryEagerSurfaceConfig.refresh_after_min``'s ``0`` default.

    A ``frozen_at`` that fails to parse is treated as stale rather than
    raised: ``from_metadata`` already keeps a snapshot with a bad
    ``frozen_at`` from being constructed, but this is a second, independent
    guard so this function itself never raises regardless of how the
    snapshot it's given was built.
    """
    if refresh_after_min <= 0:
        return False
    reference = now if now is not None else datetime.now(timezone.utc)
    try:
        frozen_at = datetime.fromisoformat(snap.frozen_at)
    except (ValueError, TypeError):
        return True
    age_minutes = (reference - frozen_at).total_seconds() / 60
    return age_minutes > refresh_after_min
