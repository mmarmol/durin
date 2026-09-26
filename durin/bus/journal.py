"""Inbound messages a process still owed a turn to when it stopped.

The bus and the per-session pending queues are in-memory. A turn in flight
keeps later same-session messages in its pending queues and re-publishes
them to the bus on its way out, but a stopping process never consumes that
bus again, so every restart with a turn in flight discarded those follow-ups
— and no channel redelivers them (Telegram confirms its offset before the
handler runs, Slack acks the envelope before publishing, email marks the
message seen inside the fetch). The journal is the bridge across the restart:
the loop writes the owed messages here at shutdown and replays them into the
bus at the next start.

One journal file is shared by every process that can run an ``AgentLoop``
against a workspace — the gateway, but also the TUI and the legacy REPL when
run locally against the same ``DURIN_HOME``. Without partitioning, a gateway
starting while a TUI has just journaled its own turns would replay them (and
the reverse), stealing a turn that belongs to a different process's channels.
So each entry is written with its writer's ``kind`` (an opaque caller-chosen
label — the gateway passes none, the TUI/REPL pass ``"tui"``), and a drain
only takes the entries tagged for the replaying process's own kind, or
untagged (a file written before this existed, or a caller that does not
distinguish process kinds) — the rest are rewritten back untouched, for
their own process's own next start. ``append`` and ``drain`` both take
``cross_process_lock`` on the file, so a concurrent writer during a drain
waits instead of racing a read-modify-write.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from loguru import logger

from durin.bus.events import InboundMessage
from durin.utils.file_lock import cross_process_lock

# A message older than this at replay time is dropped: an answer to a
# question asked a day ago, delivered out of the blue, is worse than none.
DEFAULT_MAX_AGE_S = 24 * 3600

# Journal-record key holding the writer's process kind. Leading underscore
# keeps it visibly separate from the InboundMessage fields it rides next to.
_KIND_KEY = "_writer_kind"


def _to_record(msg: InboundMessage) -> dict:
    record = asdict(msg)
    record["timestamp"] = msg.timestamp.isoformat()
    return record


def _from_record(record: dict) -> InboundMessage:
    fields = dict(record)
    raw_ts = fields.pop("timestamp", None)
    msg = InboundMessage(**fields)
    if isinstance(raw_ts, str):
        msg.timestamp = datetime.fromisoformat(raw_ts)
    return msg


class InboundJournal:
    """Append-then-drain file of inbound messages, one JSON object per line.

    Shared by every process on this workspace — see the module docstring for
    why ``append``/``drain`` are locked and partitioned by ``kind``.
    """

    def __init__(self, path: Path, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> None:
        self.path = Path(path)
        self.max_age_s = max_age_s

    def append(self, messages: Iterable[InboundMessage], *, kind: str | None = None) -> int:
        """Persist ``messages`` in order, tagged with the writer's ``kind``
        (``None`` — the default — for a caller that does not distinguish
        process kinds; a later ``drain`` treats that as matching any kind).
        Returns how many were written; an empty batch creates no file and
        takes no lock. Locked against a concurrent append or drain on this
        same file."""
        records = [_to_record(m) for m in messages]
        if not records:
            return 0
        for record in records:
            record[_KIND_KEY] = kind
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(self.path):
            with self.path.open("a", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(records)

    def drain(self, *, kind: str | None = None) -> list[InboundMessage]:
        """Read every journaled message tagged for ``kind`` (or carrying no
        kind at all) that is still young enough to replay, and remove those
        lines — plus every OTHER line past the age cutoff regardless of ITS
        kind. A still-fresh line tagged for a DIFFERENT kind is the only
        thing left in the file, untouched, for that other process's own
        next drain. ``kind=None`` (the default) matches every entry
        regardless of its own tag, the same as before this existed.

        The age cutoff applies to every line, not only the ones this call
        would otherwise take: without that, a line belonging to a process
        kind that never runs again (or a `kind=None` caller update that
        left old entries behind) would be read and rewritten back on every
        single drain, forever. A message is replayed at most once: the
        lines this call takes are removed before returning, even if replay
        itself is never attempted. A line that does not parse is logged and
        dropped either way. Locked against a concurrent append or drain on
        this same file.
        """
        with cross_process_lock(self.path):
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
            messages: list[InboundMessage] = []
            kept_lines: list[str] = []
            cutoff = datetime.now().timestamp() - self.max_age_s
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                    if not isinstance(raw, dict):
                        raise TypeError(f"expected a JSON object, got {type(raw).__name__}")
                except (ValueError, TypeError) as exc:
                    logger.warning("inbound journal: skipping unreadable line ({}): {}", exc, stripped[:120])
                    continue
                entry_kind = raw.pop(_KIND_KEY, None)
                # Read the timestamp straight off the raw record (not via
                # _from_record) so aging a line we may only be KEEPING, not
                # taking, never requires it to satisfy the full InboundMessage
                # shape — a kept line must stay exactly as untouched as its
                # own next drain would find it, malformed-but-fresh included.
                raw_ts = raw.get("timestamp")
                try:
                    entry_time = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else None
                except ValueError:
                    entry_time = None
                if entry_time is not None and entry_time.timestamp() < cutoff:
                    logger.info(
                        "inbound journal: dropping a message from {} older than {}s "
                        "(session {}, kind {})",
                        entry_time.isoformat(timespec="minutes"), int(self.max_age_s),
                        raw.get("session_key"), entry_kind,
                    )
                    continue
                if kind is not None and entry_kind is not None and entry_kind != kind:
                    kept_lines.append(stripped)   # another process's own, still-fresh entry
                    continue
                try:
                    msg = _from_record(raw)
                except (ValueError, TypeError) as exc:
                    logger.warning("inbound journal: skipping unreadable line ({}): {}", exc, stripped[:120])
                    continue
                messages.append(msg)
            if kept_lines:
                self.path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
            else:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            return messages
