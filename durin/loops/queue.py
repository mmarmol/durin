"""Per-loop event queue: <workspace>/loops/queue/<loop>.jsonl.

Holds inbound channel events a ``single``-concurrency loop couldn't fire on
immediately because a run was already active (see
``durin.loops.matcher.TriggerMatcher._dispatch_match``). ``runtime._post_finish``
drains the queue once the loop frees up.

One append-only file per loop; push/pop/pending all take the same
per-loop ``cross_process_lock`` (the matcher and the runtime's drain hook can
run in different processes and overlap on the same loop's queue).

Event dicts are opaque payloads to this module beyond the ``queued_at``
timestamp it owns: ``push`` stamps arrival time itself (via ``setdefault``,
so a caller-supplied ``queued_at`` is respected) and ``pop_fresh`` measures
staleness against it. Freshness is decided at pop time against the *caller's*
``ttl_s``, not a value baked in at enqueue time — a queue_ttl_s config change
takes effect immediately for events already sitting in the queue.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock


def _dir(workspace: str | Path) -> Path:
    return Path(workspace) / "loops" / "queue"


def _path(workspace: str | Path, loop: str) -> Path:
    return _dir(workspace) / f"{loop}.jsonl"


def _read_events(p: Path) -> list[dict]:
    if not p.exists():
        return []
    events: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue  # malformed line skipped, never fatal
        if isinstance(rec, dict):
            events.append(rec)
    return events


def _write_events(p: Path, events: list[dict]) -> None:
    if not events:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(e) for e in events) + "\n"
    atomic_write_text(p, text, encoding="utf-8")


def push(workspace: str | Path, loop: str, event: dict) -> None:
    """Append *event* to the loop's queue file. Stamps ``queued_at`` (via
    ``setdefault``) if the caller didn't already set one."""
    record = dict(event)
    record.setdefault("queued_at", time.time())
    p = _path(workspace, loop)
    with cross_process_lock(_dir(workspace) / loop):
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def pop_fresh(workspace: str | Path, loop: str, ttl_s: int) -> dict | None:
    """Return the oldest event younger than ``ttl_s``, removing it from the
    file. Expired entries are dropped permanently as a side effect, whether
    or not a fresh event is found."""
    p = _path(workspace, loop)
    with cross_process_lock(_dir(workspace) / loop):
        events = _read_events(p)
        now = time.time()
        fresh = [e for e in events if now - float(e.get("queued_at") or 0) <= ttl_s]
        if not fresh:
            _write_events(p, [])
            return None
        popped, remaining = fresh[0], fresh[1:]
        _write_events(p, remaining)
        return popped


def pending(workspace: str | Path, loop: str) -> int:
    """Count of (parseable) events currently queued, TTL not applied."""
    with cross_process_lock(_dir(workspace) / loop):
        return len(_read_events(_path(workspace, loop)))
