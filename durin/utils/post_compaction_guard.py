"""Post-compaction loop guard (OpenClaw-inspired Tier 2 C2).

durin's 1A loop-detection blocks repeats of the EXACT ``(tool_name, args)``
pair after a known FAILURE. It does NOT catch a different failure mode:
the model has fixated on a tool that *succeeds* but doesn't make progress —
e.g. repeatedly reading the same file, getting the same content, but unable
to act on what it sees. Consolidation is the runtime's natural "reset" event
(summarises history, frees context, gives the model a clean slate). When the
SAME `(tool_name, args, result)` triple repeats *after* a successful
consolidation, that's a strong signal the loop is structural and not fixable
by the model alone — abort.

OpenClaw arms the guard for ``window_size`` tool calls (default 3) after a
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

# Mirrors OpenClaw's DEFAULT_WINDOW_SIZE in post-compaction-loop-guard.ts.
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
        if not session_key or self.window_size <= 0:
            return Verdict(should_abort=False, armed_after=False, remaining_attempts=0)
        slot = self._slots.get(session_key)
        if slot is None or slot.remaining_attempts <= 0:
            return Verdict(should_abort=False, armed_after=False, remaining_attempts=0)

        slot.remaining_attempts -= 1
        slot.history.append(observation)
        armed_after = slot.remaining_attempts > 0

        matches = sum(
            1 for entry in slot.history
            if entry.tool_name == observation.tool_name
            and entry.args_hash == observation.args_hash
            and entry.result_hash == observation.result_hash
        )

        if matches >= self.window_size:
            # Trip: clear the slot so subsequent calls don't keep observing
            # against this already-burned arming. A fresh compaction would
            # re-arm explicitly.
            self._slots.pop(session_key, None)
            return Verdict(
                should_abort=True,
                armed_after=False,
                remaining_attempts=0,
                repeat_count=matches,
                tool_name=observation.tool_name,
            )

        if not armed_after:
            # Window exhausted without a trip → drop the slot.
            self._slots.pop(session_key, None)

        return Verdict(
            should_abort=False,
            armed_after=armed_after,
            remaining_attempts=slot.remaining_attempts,
        )

    def reset(self, session_key: str | None) -> None:
        if not session_key:
            return
        self._slots.pop(session_key, None)
