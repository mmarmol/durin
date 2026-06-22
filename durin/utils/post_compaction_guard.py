"""Post-compaction loop guard: prevents infinite loops after consolidation.

Loop detection blocks repeats of the EXACT ``(tool_name, args)`` pair after
a known FAILURE. It does NOT catch a different failure mode: the model has
fixated on a tool that *succeeds* but doesn't make progress — e.g. repeatedly
reading the same file, getting the same content, but unable to act on what it
sees. Consolidation is the runtime's natural "reset" event (summarises
history, frees context, gives the model a clean slate). When the SAME
`(tool_name, args, result)` triple repeats *after* a successful consolidation,
that's a strong signal the loop is structural and not fixable by the model
alone — abort.

The guard is armed for ``window_size`` tool calls (default 3) after a
successful compaction. Within the window, every observation is stored; once
``window_size`` matches of the same triple are seen, the guard trips. After
the window expires (or trips), the guard returns to disarmed.

This is deliberately narrow:
- It only fires WITHIN the window (window=3 by default → first 3 tool calls).
- It requires the FULL triple to match (a different argument or a different
  result resets — we want exact repetition).
- It's per-session — different sessions don't share state.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

# Default window size for the post-compaction guard.
_DEFAULT_WINDOW_SIZE = 3


def _window_size_setting() -> int:
    raw = os.getenv("DURIN_POST_COMPACTION_GUARD_WINDOW")
    if raw is None:
        return _DEFAULT_WINDOW_SIZE
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_WINDOW_SIZE
    # Negative / zero disables the guard.
    return max(0, value)


def _stable_hash(value: Any) -> str:
    """16-char SHA-256 prefix over a JSON-stable representation. Falls
    back to ``repr`` for non-serialisable values."""
    try:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = repr(value)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def hash_args(arguments: Any) -> str:
    return _stable_hash(arguments)


def hash_result(result: Any) -> str:
    return _stable_hash(result)


@dataclass(slots=True)
class Observation:
    tool_name: str
    args_hash: str
    result_hash: str


@dataclass(slots=True)
class _GuardSlot:
    """Per-session state. Reset at each successful arm."""
    remaining_attempts: int = 0
    history: list[Observation] = field(default_factory=list)


@dataclass(slots=True)
class Verdict:
    """Outcome of one ``observe`` call.

    ``should_abort=True`` → the runner must terminate the turn with a
    distinct stop_reason. ``armed_after`` indicates whether the guard is
    still armed for further observations after this one (always False
    once it trips).
    """
    should_abort: bool
    armed_after: bool
    remaining_attempts: int
    repeat_count: int = 0
    tool_name: str = ""


class PostCompactionLoopGuard:
    """Stateful guard, one instance per ``Consolidator``.

    Lifecycle:
    1. After a successful compaction round, the consolidator calls
       :meth:`arm` with the affected session key.
    2. The runner calls :meth:`observe` after each tool execution.
    3. If a triple ``(tool_name, args_hash, result_hash)`` is seen
       ``window_size`` times within the window, :meth:`observe` returns
       a verdict with ``should_abort=True``.
    4. After ``window_size`` tool calls without a trip, the guard
       auto-disarms.

    Sessions are tracked independently — arming one session does not
    affect another.
    """

    def __init__(self, *, window_size: int | None = None) -> None:
        if window_size is None:
            window_size = _window_size_setting()
        self.window_size = max(0, window_size)
        self._slots: dict[str, _GuardSlot] = {}

    def arm(self, session_key: str | None) -> None:
        if not session_key or self.window_size <= 0:
            return
        self._slots[session_key] = _GuardSlot(
            remaining_attempts=self.window_size,
            history=[],
        )

    def is_armed(self, session_key: str | None) -> bool:
        if not session_key:
            return False
        slot = self._slots.get(session_key)
        return bool(slot and slot.remaining_attempts > 0)

    def observe(
        self,
        session_key: str | None,
        observation: Observation,
    ) -> Verdict:
        """Record one tool call against the active window for this
        session. Two independent decisions happen here:

        1. **Track the attempt** — decrement ``remaining_attempts`` and
           append to history. This burns a slot in the window regardless
           of whether the call matches.
        2. **Check for trip** — count how many entries in the (now-
           updated) history are identical to this observation. If the
           count reaches ``window_size``, the guard trips.

        Concretely, with ``window_size=3`` and three identical triples:

        - Call 1: attempts 3→2, history=[A], matches(A)=1, no trip.
        - Call 2: attempts 2→1, history=[A,A], matches(A)=2, no trip.
        - Call 3: attempts 1→0, history=[A,A,A], matches(A)=3, **TRIP**.

        With three *different* triples: matches stays at 1 each turn,
        no trip, window exhausts on call 3 and auto-disarms.
        """
        if not self._is_armed_slot(session_key):
            return Verdict(should_abort=False, armed_after=False, remaining_attempts=0)

        slot = self._slots[session_key]  # safe: _is_armed_slot returned True
        self._track_attempt(slot, observation)

        matches = self._count_matches(slot, observation)
        if matches >= self.window_size:
            self._slots.pop(session_key, None)
            return Verdict(
                should_abort=True,
                armed_after=False,
                remaining_attempts=0,
                repeat_count=matches,
                tool_name=observation.tool_name,
            )

        armed_after = slot.remaining_attempts > 0
        if not armed_after:
            # Window exhausted without a trip → drop the slot so the
            # next ``observe`` call cleanly returns "not armed".
            self._slots.pop(session_key, None)
        return Verdict(
            should_abort=False,
            armed_after=armed_after,
            remaining_attempts=slot.remaining_attempts,
        )

    def _is_armed_slot(self, session_key: str | None) -> bool:
        """Internal: True iff the guard is enabled, the key is non-empty,
        and a slot with remaining attempts exists for this session."""
        if not session_key or self.window_size <= 0:
            return False
        slot = self._slots.get(session_key)
        return slot is not None and slot.remaining_attempts > 0

    @staticmethod
    def _track_attempt(slot: _GuardSlot, observation: Observation) -> None:
        """Internal: burn one slot in the window. Always called when
        ``_is_armed_slot`` returned True."""
        slot.remaining_attempts -= 1
        slot.history.append(observation)

    @staticmethod
    def _count_matches(slot: _GuardSlot, observation: Observation) -> int:
        """Internal: how many entries in ``slot.history`` are identical
        to ``observation`` (same name, args_hash, result_hash)? The
        history was already appended by ``_track_attempt``, so a match
        always counts at least the current observation itself."""
        return sum(
            1 for entry in slot.history
            if entry.tool_name == observation.tool_name
            and entry.args_hash == observation.args_hash
            and entry.result_hash == observation.result_hash
        )

    def reset(self, session_key: str | None) -> None:
        if not session_key:
            return
        self._slots.pop(session_key, None)
