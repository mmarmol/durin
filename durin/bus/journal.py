"""Inbound messages the gateway still owed a turn to when it stopped.

The bus and the per-session pending queues are in-memory. A turn in flight
keeps later same-session messages in its pending queues and re-publishes
them to the bus on its way out, but a stopping gateway never consumes that
bus again, so every restart with a turn in flight discarded those follow-ups
— and no channel redelivers them (Telegram confirms its offset before the
handler runs, Slack acks the envelope before publishing, email marks the
message seen inside the fetch). The journal is the bridge across the restart:
the loop writes the owed messages here at shutdown and replays them into the
bus at the next start.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from loguru import logger

from durin.bus.events import InboundMessage

# A message older than this at replay time is dropped: an answer to a
# question asked a day ago, delivered out of the blue, is worse than none.
DEFAULT_MAX_AGE_S = 24 * 3600


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
    """Append-then-drain file of inbound messages, one JSON object per line."""

    def __init__(self, path: Path, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> None:
        self.path = Path(path)
        self.max_age_s = max_age_s

    def append(self, messages: Iterable[InboundMessage]) -> int:
        """Persist ``messages`` in order. Returns how many were written; an
        empty batch creates no file."""
        records = [_to_record(m) for m in messages]
        if not records:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(records)

    def drain(self) -> list[InboundMessage]:
        """Read every journaled message still young enough to replay and
        delete the file, so a message is replayed at most once. A line that
        does not parse into a message is logged and skipped; the rest are
        kept."""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        finally:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        messages: list[InboundMessage] = []
        cutoff = datetime.now().timestamp() - self.max_age_s
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = _from_record(json.loads(line))
            except (ValueError, TypeError) as exc:
                logger.warning("inbound journal: skipping unreadable line ({}): {}", exc, line[:120])
                continue
            if msg.timestamp.timestamp() < cutoff:
                logger.info(
                    "inbound journal: dropping a message from {} older than {}s (session {})",
                    msg.timestamp.isoformat(timespec="minutes"), int(self.max_age_s), msg.session_key,
                )
                continue
            messages.append(msg)
        return messages
