"""Read primitive shared by the gateway + telemetry log viewer tabs.

The filesystem is the time index: files rotate by time and lines within a
file are append-ordered (ascending ts). Reads go newest-file-first, grep
before json.loads, paginate by a ``before_ts`` cursor, decompress ``.gz``
transparently, and bound the scan by a time window. No database, no index.
"""
from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

__all__ = [
    "LogLine",
    "parse_line",
    "open_text",
    "session_from_filename",
]

_SESSION_DATE_RE = re.compile(r"^(?P<session>.+)_\d{4}-\d{2}-\d{2}$")


@dataclass
class LogLine:
    ts: float
    text: str                    # raw line text (for substring search)
    fields: dict                 # normalized fields for filters/display
    raw: dict                    # full parsed object (row expansion)


def parse_line(source: str, text: str, *, session: str | None) -> LogLine | None:
    """Parse one JSONL line for *source*. Returns None on malformed lines."""
    text = text.rstrip("\n")
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if source == "gateway":
        rec = obj.get("record")
        if not isinstance(rec, dict):
            return None
        try:
            ts = float(rec["time"]["timestamp"])
        except (KeyError, TypeError, ValueError):
            return None
        fields = {
            "level": (rec.get("level") or {}).get("name", "-"),
            "channel": (rec.get("extra") or {}).get("channel", "-"),
            "message": rec.get("message", ""),
        }
        return LogLine(ts=ts, text=text, fields=fields, raw=obj)
    # telemetry
    try:
        ts = float(obj["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    fields = {
        "type": obj.get("type", "-"),
        "session": session or "-",
        "data": obj.get("data", {}),
    }
    return LogLine(ts=ts, text=text, fields=fields, raw=obj)


def open_text(path: Path) -> Iterator[str]:
    """Yield text lines from *path*, transparently decompressing ``.gz``."""
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            yield from fh
    else:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            yield from fh


def session_from_filename(name: str) -> str:
    """Derive the telemetry session key from ``{session}_{date}.jsonl[.gz]``."""
    base = name
    for suffix in (".jsonl.gz", ".jsonl"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    m = _SESSION_DATE_RE.match(base)
    return m.group("session") if m else base
