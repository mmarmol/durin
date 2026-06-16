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
    "LogQuery",
    "LogPage",
    "parse_line",
    "open_text",
    "session_from_filename",
    "segment_files",
    "read_page",
    "compute_facets",
]

_GATEWAY_LEVELS = ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]

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


@dataclass
class LogQuery:
    source: str                                  # "gateway" | "telemetry"
    q: str | None = None                         # substring (raw-text grep)
    before_ts: float | None = None               # cursor: only lines with ts < before_ts
    window_hours: float | None = 24.0            # bound the scan; None = unbounded
    limit: int = 200
    filters: dict[str, set[str]] = field(default_factory=dict)  # field -> allowed values
    max_scan_lines: int = 500_000               # hard budget so a query can't run away


@dataclass
class LogPage:
    lines: list[dict]
    next_cursor: float | None
    scanned_through_ts: float | None
    has_more: bool


def segment_files(directory: Path, source: str) -> list[Path]:
    """Return matching segment files, NEWEST FIRST (by mtime)."""
    if not directory.is_dir():
        return []
    if source == "gateway":
        names = list(directory.glob("gateway.log")) + list(directory.glob("gateway.*log*"))
        # Exclude the raw boot capture — it is NOT JSONL (plain stdout/stderr).
        names = [p for p in names if ".boot." not in p.name]
    else:
        names = list(directory.glob("*.jsonl")) + list(directory.glob("*.jsonl.gz"))
    uniq = {p.resolve(): p for p in names if p.is_file()}
    return sorted(uniq.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def _passes_filters(line: LogLine, filters: dict[str, set[str]]) -> bool:
    for key, allowed in filters.items():
        if not allowed:
            continue
        if str(line.fields.get(key, "")) not in allowed:
            return False
    return True


def _newest_ts(directory: Path, source: str) -> float | None:
    for path in segment_files(directory, source):
        session = session_from_filename(path.name) if source == "telemetry" else None
        for text in reversed(list(open_text(path))):
            line = parse_line(source, text, session=session)
            if line is not None:
                return line.ts
    return None


def read_page(directory: Path, query: LogQuery) -> LogPage:
    """Stream matching lines newest-first, paginated by a before_ts cursor."""
    q = (query.q or "").lower() or None

    floor_ts = None
    if query.window_hours is not None:
        anchor = query.before_ts if query.before_ts is not None else _newest_ts(directory, query.source)
        if anchor is not None:
            floor_ts = anchor - query.window_hours * 3600.0

    collected: list[LogLine] = []
    scanned = 0
    scanned_through_ts: float | None = None
    has_more = False
    hit_window = False

    for path in segment_files(directory, query.source):
        session = session_from_filename(path.name) if query.source == "telemetry" else None
        for text in reversed(list(open_text(path))):
            scanned += 1
            if scanned > query.max_scan_lines:
                has_more = True
                break
            if q is not None and q not in text.lower():
                continue
            line = parse_line(query.source, text, session=session)
            if line is None:
                continue
            scanned_through_ts = (
                line.ts if scanned_through_ts is None else min(scanned_through_ts, line.ts)
            )
            if query.before_ts is not None and line.ts >= query.before_ts:
                continue
            if floor_ts is not None and line.ts < floor_ts:
                hit_window = True
                continue
            if not _passes_filters(line, query.filters):
                continue
            collected.append(line)
            if len(collected) > query.limit:
                has_more = True
                break
        if has_more:
            break

    if len(collected) > query.limit:
        collected = collected[: query.limit]
        has_more = True
    next_cursor = collected[-1].ts if (has_more and collected) else None
    if hit_window and not collected:
        has_more = True  # nothing in window but older data exists -> offer widen
    return LogPage(
        lines=[{"ts": e.ts, "fields": e.fields, "raw": e.raw} for e in collected],
        next_cursor=next_cursor,
        scanned_through_ts=scanned_through_ts,
        has_more=has_more,
    )


def compute_facets(directory: Path, source: str) -> dict[str, list[str]]:
    """Filter options derived cheaply from filenames / static registries."""
    if source == "gateway":
        channels: set[str] = set()
        segs = segment_files(directory, "gateway")
        if segs:  # newest segment only — channels are a small, recent set
            for text in open_text(segs[0]):
                line = parse_line("gateway", text, session=None)
                if line is not None:
                    channels.add(str(line.fields.get("channel", "-")))
        return {"levels": _GATEWAY_LEVELS, "channels": sorted(channels)}
    # telemetry: sessions from filenames, types from the static registry
    sessions = {session_from_filename(p.name) for p in segment_files(directory, "telemetry")}
    try:
        from durin.telemetry.schema import EVENTS
        types = sorted(EVENTS.keys())
    except Exception:  # noqa: BLE001
        types = []
    return {"sessions": sorted(sessions), "types": types}
