# Logs viewer + gateway lifecycle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only web "Logs" viewer (Gateway + Telemetry tabs) and convert the gateway log into a rotating/compressing JSONL file with web-editable retention — without touching the telemetry backend.

**Architecture:** The gateway log moves to a loguru JSONL file sink (size rotation + gz compression + day retention). A single server-side read primitive (`durin/logs/reader.py`) streams newest-file-first, greps-before-parsing, paginates by a `before_ts` cursor, decompresses `.gz` transparently, and bounds scans by a time window. One `/api/logs` endpoint serves both tabs; they differ only in directory, line parser, and facet source.

**Tech Stack:** Python 3 / loguru / aiohttp-style WS channel / Pydantic / pytest · React + TypeScript / Vite / lucide-react.

**Spec:** `docs/superpowers/specs/2026-06-03-logs-viewer-lifecycle-design.md`

**Out of scope / DO NOT TOUCH:** `durin/telemetry/logger.py`, `durin/telemetry/retention.py`.

---

## Phase 1 — Logging config

### Task 1: `LoggingConfig` Pydantic model

**Files:**
- Modify: `durin/config/schema.py` (add class near `TelemetryConfig` ~line 782; add field to `Config` ~line 800)
- Test: `tests/config/test_logging_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_logging_config.py
from durin.config.schema import Config


def test_logging_defaults():
    cfg = Config()
    assert cfg.logging.max_file_mb == 5
    assert cfg.logging.retention_days == 7


def test_logging_camel_and_snake_aliases():
    cfg = Config.model_validate({"logging": {"maxFileMb": 10, "retention_days": 3}})
    assert cfg.logging.max_file_mb == 10
    assert cfg.logging.retention_days == 3


def test_logging_bounds():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Config.model_validate({"logging": {"max_file_mb": 0}})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/config/test_logging_config.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'logging'`

- [ ] **Step 3: Add the model + field**

In `durin/config/schema.py`, after the `TelemetryConfig` class (~line 790):

```python
class LoggingConfig(Base):
    """Gateway daemon log lifecycle (web-editable).

    Governs ONLY the gateway's ``gateway.log`` file sink — rotation by
    size, gz compression of rotated segments, deletion by age. The
    telemetry subsystem has its own independent lifecycle
    (``durin/telemetry/retention.py``) and is NOT affected by these keys.
    """

    max_file_mb: int = Field(
        default=5, ge=1, le=1024,
        validation_alias=AliasChoices("maxFileMb", "max_file_mb"),
        serialization_alias="maxFileMb",
    )  # Size at which gateway.log rotates to a new segment.
    retention_days: int = Field(
        default=7, ge=1, le=365,
        validation_alias=AliasChoices("retentionDays", "retention_days"),
        serialization_alias="retentionDays",
    )  # Age at which rotated gateway segments are deleted.
```

In the `Config` class body (~line 800, alongside `telemetry:`), add:

```python
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/config/test_logging_config.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add durin/config/schema.py tests/config/test_logging_config.py
git commit -m "feat(config): add logging.max_file_mb / retention_days"
```

---

## Phase 2 — Gateway JSONL file sink (lifecycle)

### Task 2: `configure_gateway_file_logging` helper

**Files:**
- Create: `durin/cli/gateway_logging.py`
- Test: `tests/cli/test_gateway_logging.py`

Why a new module: keeps the loguru sink wiring testable in isolation, away from the large `commands.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_gateway_logging.py
import gzip
import json
from pathlib import Path

from loguru import logger

from durin.cli.gateway_logging import configure_gateway_file_logging


def test_file_sink_writes_jsonl(tmp_path: Path):
    log_file = tmp_path / "gateway.log"
    sink_id = configure_gateway_file_logging(log_file, max_file_mb=5, retention_days=7)
    try:
        logger.bind(channel="test").info("hello world")
    finally:
        logger.remove(sink_id)
    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    obj = json.loads(line)
    assert obj["record"]["message"] == "hello world"
    assert obj["record"]["level"]["name"] == "INFO"
    assert obj["record"]["extra"]["channel"] == "test"


def test_rotation_by_size_and_gz(tmp_path: Path):
    log_file = tmp_path / "gateway.log"
    # 1 MB threshold so the test rotates quickly.
    sink_id = configure_gateway_file_logging(log_file, max_file_mb=1, retention_days=7)
    try:
        for i in range(8000):
            logger.bind(channel="bulk").info("x" * 200 + f" {i}")
    finally:
        logger.remove(sink_id)
    rotated = list(tmp_path.glob("gateway.*.log.gz")) + list(tmp_path.glob("gateway.log.*.gz"))
    assert rotated, "expected at least one gz-compressed rotated segment"
    # gz is readable and contains JSONL.
    with gzip.open(rotated[0], "rt", encoding="utf-8") as fh:
        first = json.loads(fh.readline())
    assert "record" in first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cli/test_gateway_logging.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'durin.cli.gateway_logging'`

- [ ] **Step 3: Implement the helper**

```python
# durin/cli/gateway_logging.py
"""Gateway log file sink: JSONL + size rotation + gz + age retention.

The gateway's structured log lands in ``gateway.log`` as one JSON line
per event (loguru ``serialize=True``). Rotated segments are gz-compressed
and deleted past ``retention_days``. The human-readable stderr sink set up
elsewhere is untouched — this adds the FILE sink only.
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

__all__ = ["configure_gateway_file_logging", "install_excepthook"]


def configure_gateway_file_logging(
    log_file: Path,
    *,
    max_file_mb: int,
    retention_days: int,
) -> int:
    """Add a JSONL rotating/compressing file sink. Returns the sink id."""
    return logger.add(
        str(log_file),
        serialize=True,                      # one JSON object per line
        rotation=f"{max_file_mb} MB",
        retention=f"{retention_days} days",
        compression="gz",                    # rotated segments -> .gz
        level="INFO",
        enqueue=True,                        # process/thread-safe writes
        backtrace=False,
        diagnose=False,
        filter=lambda record: record["extra"].setdefault("channel", "-") or True,
    )


def install_excepthook() -> None:
    """Route uncaught exceptions to loguru so they land in the JSONL sink."""
    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.bind(channel="-").opt(exception=(exc_type, exc_value, exc_tb)).error(
            "uncaught exception"
        )

    sys.excepthook = _hook
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cli/test_gateway_logging.py -v`
Expected: PASS (2 tests). If the rotated glob misses, inspect `tmp_path` contents and adjust the glob in the test to loguru's actual naming (`gateway.<timestamp>.log.gz`).

- [ ] **Step 5: Commit**

```bash
git add durin/cli/gateway_logging.py tests/cli/test_gateway_logging.py
git commit -m "feat(logs): gateway JSONL file sink with size rotation + gz + retention"
```

### Task 3: Wire the sink into the gateway run (gated) + boot.log redirect

**Files:**
- Modify: `durin/cli/gateway_daemon.py` (`daemon_logs_path` neighbourhood ~line 58; `start_daemon` ~line 150-167)
- Modify: `durin/cli/commands.py` (gateway run path; uses `_load_runtime_config`)
- Test: `tests/cli/test_gateway_daemon_logging.py`

- [ ] **Step 1: Write the failing test (boot path helper)**

```python
# tests/cli/test_gateway_daemon_logging.py
from durin.cli.gateway_daemon import daemon_boot_logs_path, daemon_logs_path


def test_boot_path_is_sibling_of_log(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert daemon_boot_logs_path().name == "gateway.boot.log"
    assert daemon_boot_logs_path().parent == daemon_logs_path().parent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cli/test_gateway_daemon_logging.py -v`
Expected: FAIL — `ImportError: cannot import name 'daemon_boot_logs_path'`

- [ ] **Step 3a: Add `daemon_boot_logs_path` and the env flag constant**

In `durin/cli/gateway_daemon.py`, after `daemon_logs_path` (~line 61):

```python
GATEWAY_LOG_FILE_ENV = "DURIN_GATEWAY_LOG_FILE"  # set by start_daemon; read by the gateway run


def daemon_boot_logs_path() -> Path:
    """Raw stdout/stderr capture for the daemon child (truncated each start).

    Catches catastrophic pre-loguru failures (import errors, early
    tracebacks). The structured log lives in ``gateway.log`` (loguru-owned,
    rotating); this file is only a boot-time safety net.
    """
    return _state_root() / "logs" / "gateway.boot.log"
```

Add `"daemon_boot_logs_path"` and `"GATEWAY_LOG_FILE_ENV"` to `__all__`.

- [ ] **Step 3b: Redirect child IO to boot.log + set the env flag**

In `start_daemon` (~line 150-167), replace the `log_fd`/`Popen` block:

```python
    boot_path = daemon_boot_logs_path()
    boot_path.parent.mkdir(parents=True, exist_ok=True)
    # Truncate each start: boot.log only holds the current boot's raw output.
    log_fd = open(boot_path, "wb", buffering=0)  # noqa: SIM115 — dup'd by the child
    try:
        binary = durin_executable or _resolve_durin_binary()
        cmd = [binary, "gateway", "--foreground", *(extra_args or [])]
        env = {**os.environ, GATEWAY_LOG_FILE_ENV: str(daemon_logs_path())}
        proc = subprocess.Popen(  # noqa: S603 — durin invokes its own binary; no shell
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        log_fd.close()
```

- [ ] **Step 3c: Add the sink in the gateway run path**

In `durin/cli/commands.py`, in the gateway run function (the `gateway_app` callback that calls `_load_runtime_config`), right after the runtime config is loaded and before the gateway starts serving, add:

```python
    import os as _os
    from durin.cli.gateway_daemon import GATEWAY_LOG_FILE_ENV, daemon_logs_path
    from durin.cli.gateway_logging import (
        configure_gateway_file_logging,
        install_excepthook,
    )

    if _os.environ.get(GATEWAY_LOG_FILE_ENV):
        configure_gateway_file_logging(
            daemon_logs_path(),
            max_file_mb=runtime_config.logging.max_file_mb,
            retention_days=runtime_config.logging.retention_days,
        )
        install_excepthook()
```

(Find the gateway callback by searching `commands.py` for `gateway_app` / the function decorated with `@gateway_app.callback()`. Place the block after `runtime_config = _load_runtime_config(...)`.)

- [ ] **Step 4: Run test + manual smoke**

Run: `pytest tests/cli/test_gateway_daemon_logging.py -v`
Expected: PASS.
Manual smoke (optional here, full live check in Phase 6): `HOME=/tmp/empty GIT_CONFIG_NOSYSTEM=1 python -c "from durin.cli.gateway_daemon import daemon_boot_logs_path; print(daemon_boot_logs_path())"` → prints a path, no crash.

- [ ] **Step 5: Commit**

```bash
git add durin/cli/gateway_daemon.py durin/cli/commands.py tests/cli/test_gateway_daemon_logging.py
git commit -m "feat(logs): wire gateway JSONL sink (gated) + boot.log raw capture"
```

---

## Phase 3 — Shared read primitive

### Task 4: `LogLine` parsing (gateway + telemetry)

**Files:**
- Create: `durin/logs/__init__.py` (empty)
- Create: `durin/logs/reader.py`
- Test: `tests/logs/test_reader_parse.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logs/test_reader_parse.py
import json

from durin.logs.reader import parse_line


def test_parse_gateway_loguru_jsonl():
    raw = json.dumps({
        "text": "...",
        "record": {
            "time": {"timestamp": 1717430000.5},
            "level": {"name": "ERROR"},
            "extra": {"channel": "telegram"},
            "message": "boom",
        },
    })
    line = parse_line("gateway", raw, session=None)
    assert line is not None
    assert line.ts == 1717430000.5
    assert line.fields["level"] == "ERROR"
    assert line.fields["channel"] == "telegram"
    assert line.fields["message"] == "boom"


def test_parse_telemetry_jsonl():
    raw = json.dumps({"ts": 1717430001.0, "type": "memory.dream_start", "data": {"k": 1}})
    line = parse_line("telemetry", raw, session="cli_default")
    assert line.ts == 1717430001.0
    assert line.fields["type"] == "memory.dream_start"
    assert line.fields["session"] == "cli_default"


def test_parse_bad_line_returns_none():
    assert parse_line("gateway", "not json", session=None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/logs/test_reader_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'durin.logs'`

- [ ] **Step 3: Implement parsing**

```python
# durin/logs/__init__.py
```

```python
# durin/logs/reader.py
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

__all__ = ["LogLine", "parse_line", "open_text"]

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
    if source == "gateway":
        rec = obj.get("record") if isinstance(obj, dict) else None
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/logs/test_reader_parse.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add durin/logs/__init__.py durin/logs/reader.py tests/logs/test_reader_parse.py
git commit -m "feat(logs): JSONL line parsing for gateway + telemetry"
```

### Task 5: Segment discovery (newest-first) + page reader

**Files:**
- Modify: `durin/logs/reader.py`
- Test: `tests/logs/test_reader_page.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logs/test_reader_page.py
import gzip
import json
from pathlib import Path

from durin.logs.reader import LogQuery, read_page


def _write_gateway(path: Path, rows):
    lines = []
    for ts, level, channel, msg in rows:
        lines.append(json.dumps({"record": {
            "time": {"timestamp": ts}, "level": {"name": level},
            "extra": {"channel": channel}, "message": msg,
        }}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_newest_first_and_limit(tmp_path: Path):
    _write_gateway(tmp_path / "gateway.log", [
        (100.0, "INFO", "a", "old"),
        (200.0, "INFO", "a", "mid"),
        (300.0, "ERROR", "b", "new"),
    ])
    page = read_page(tmp_path, LogQuery(source="gateway", limit=2, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["new", "mid"]
    assert page.has_more is True
    assert page.next_cursor == 200.0


def test_cursor_resumes(tmp_path: Path):
    _write_gateway(tmp_path / "gateway.log", [
        (100.0, "INFO", "a", "old"),
        (200.0, "INFO", "a", "mid"),
        (300.0, "ERROR", "b", "new"),
    ])
    page = read_page(tmp_path, LogQuery(source="gateway", limit=2,
                                        before_ts=200.0, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["old"]
    assert page.has_more is False


def test_grep_and_level_filter(tmp_path: Path):
    _write_gateway(tmp_path / "gateway.log", [
        (100.0, "INFO", "a", "keep me"),
        (200.0, "ERROR", "b", "ignore"),
        (300.0, "ERROR", "b", "keep me too"),
    ])
    page = read_page(tmp_path, LogQuery(source="gateway", q="keep",
                                        filters={"level": {"ERROR"}}, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["keep me too"]


def test_reads_gz_segments(tmp_path: Path):
    plain = tmp_path / "gateway.log"
    _write_gateway(plain, [(300.0, "INFO", "a", "active")])
    gz = tmp_path / "gateway.2.log.gz"
    payload = json.dumps({"record": {"time": {"timestamp": 100.0},
                                     "level": {"name": "INFO"}, "extra": {"channel": "a"},
                                     "message": "archived"}}) + "\n"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(payload)
    page = read_page(tmp_path, LogQuery(source="gateway", limit=10, window_hours=None))
    assert [l["fields"]["message"] for l in page.lines] == ["active", "archived"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/logs/test_reader_page.py -v`
Expected: FAIL — `ImportError: cannot import name 'LogQuery'`

- [ ] **Step 3: Implement `LogQuery`, `LogPage`, segment discovery, `read_page`**

Append to `durin/logs/reader.py` (and extend `__all__` with `"LogQuery"`, `"LogPage"`, `"read_page"`, `"segment_files"`):

```python
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


def read_page(directory: Path, query: LogQuery) -> LogPage:
    from durin.logs.reader import open_text, parse_line, session_from_filename  # self

    q = (query.q or "").lower() or None
    floor_ts = None
    if query.window_hours is not None:
        anchor = query.before_ts
        if anchor is None:
            # Anchor the window on the newest line we can see.
            anchor = _newest_ts(directory, query.source)
        if anchor is not None:
            floor_ts = anchor - query.window_hours * 3600.0

    collected: list[LogLine] = []
    scanned = 0
    scanned_through_ts: float | None = None
    has_more = False
    hit_window = False

    for path in segment_files(directory, query.source):
        session = session_from_filename(path.name) if query.source == "telemetry" else None
        # Read whole segment (<=max_file_mb) then walk newest-first.
        rows = list(open_text(path))
        for text in reversed(rows):
            scanned += 1
            if scanned > query.max_scan_lines:
                has_more = True
                break
            if q is not None and q not in text.lower():
                continue
            line = parse_line(query.source, text, session=session)
            if line is None:
                continue
            scanned_through_ts = line.ts if scanned_through_ts is None else min(scanned_through_ts, line.ts)
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
        if has_more or (len(collected) > query.limit):
            break

    if len(collected) > query.limit:
        collected = collected[: query.limit]
        has_more = True
    next_cursor = collected[-1].ts if (has_more and collected) else None
    if hit_window and not collected:
        has_more = True  # nothing in window but older data exists -> offer widen
    return LogPage(
        lines=[{"ts": l.ts, "fields": l.fields, "raw": l.raw} for l in collected],
        next_cursor=next_cursor,
        scanned_through_ts=scanned_through_ts,
        has_more=has_more,
    )


def _passes_filters(line: LogLine, filters: dict[str, set[str]]) -> bool:
    for key, allowed in filters.items():
        if not allowed:
            continue
        if str(line.fields.get(key, "")) not in allowed:
            return False
    return True


def _newest_ts(directory: Path, source: str) -> float | None:
    from durin.logs.reader import open_text, parse_line, session_from_filename
    for path in segment_files(directory, source):
        session = session_from_filename(path.name) if source == "telemetry" else None
        rows = list(open_text(path))
        for text in reversed(rows):
            line = parse_line(source, text, session=session)
            if line is not None:
                return line.ts
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/logs/test_reader_page.py -v`
Expected: PASS (4 tests). Note: `window_hours=None` in tests bypasses the window so the synthetic 100-300s timestamps aren't filtered.

- [ ] **Step 5: Commit**

```bash
git add durin/logs/reader.py tests/logs/test_reader_page.py
git commit -m "feat(logs): newest-first paged reader with cursor, window, gz, grep-before-parse"
```

### Task 6: Facets (cheap — from filenames / static registries)

**Files:**
- Modify: `durin/logs/reader.py`
- Test: `tests/logs/test_reader_facets.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/logs/test_reader_facets.py
import json
from pathlib import Path

from durin.logs.reader import compute_facets


def test_gateway_facets(tmp_path: Path):
    (tmp_path / "gateway.log").write_text(
        json.dumps({"record": {"time": {"timestamp": 1.0}, "level": {"name": "INFO"},
                               "extra": {"channel": "telegram"}, "message": "m"}}) + "\n",
        encoding="utf-8")
    facets = compute_facets(tmp_path, "gateway")
    assert "ERROR" in facets["levels"]            # fixed enum
    assert "telegram" in facets["channels"]       # from newest segment


def test_telemetry_facets(tmp_path: Path):
    (tmp_path / "cli_default_2026-06-03.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "tg_42_2026-06-02.jsonl.gz").write_bytes(b"")
    facets = compute_facets(tmp_path, "telemetry")
    assert set(facets["sessions"]) == {"cli_default", "tg_42"}   # from filenames
    assert "memory.dream_start" in facets["types"]               # from EVENTS registry
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/logs/test_reader_facets.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_facets'`

- [ ] **Step 3: Implement `compute_facets`** (extend `__all__` with `"compute_facets"`)

```python
_GATEWAY_LEVELS = ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]


def compute_facets(directory: Path, source: str) -> dict[str, list[str]]:
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
    sessions = {
        session_from_filename(p.name)
        for p in segment_files(directory, "telemetry")
    }
    try:
        from durin.telemetry.schema import EVENTS
        types = sorted(EVENTS.keys())
    except Exception:  # noqa: BLE001
        types = []
    return {"sessions": sorted(sessions), "types": types}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/logs/test_reader_facets.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add durin/logs/reader.py tests/logs/test_reader_facets.py
git commit -m "feat(logs): cheap facets from filenames + static registries"
```

---

## Phase 4 — API endpoint

### Task 7: `/api/logs` handler + directory resolution

**Files:**
- Modify: `durin/channels/websocket.py` (dispatch ~line 675; new handler method)
- Test: `tests/channels/test_logs_endpoint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/channels/test_logs_endpoint.py
import json
from pathlib import Path

from durin.logs.reader import LogQuery, read_page


def test_query_from_params_builds_filters():
    # The handler turns query params into a LogQuery; here we assert the
    # contract used by the handler (helper kept importable for unit cover).
    from durin.channels.websocket import _logs_query_from_params
    q = _logs_query_from_params({
        "source": ["telemetry"], "q": ["dream"], "type": ["memory.dream_start"],
        "before_ts": ["1717430000.0"], "window_hours": ["48"], "limit": ["50"],
    })
    assert q.source == "telemetry"
    assert q.q == "dream"
    assert q.before_ts == 1717430000.0
    assert q.window_hours == 48.0
    assert q.limit == 50
    assert q.filters["type"] == {"memory.dream_start"}


def test_read_page_end_to_end(tmp_path: Path):
    (tmp_path / "gateway.log").write_text(
        json.dumps({"record": {"time": {"timestamp": 5.0}, "level": {"name": "INFO"},
                               "extra": {"channel": "a"}, "message": "hi"}}) + "\n",
        encoding="utf-8")
    page = read_page(tmp_path, LogQuery(source="gateway", window_hours=None))
    assert page.lines[0]["fields"]["message"] == "hi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/channels/test_logs_endpoint.py -v`
Expected: FAIL — `ImportError: cannot import name '_logs_query_from_params'`

- [ ] **Step 3a: Add the param→query helper (module-level in `websocket.py`)**

Place near the other `_parse_query` / `_query_first` helpers:

```python
def _logs_query_from_params(query: dict[str, list[str]]):
    """Build a LogQuery from parsed URL query params."""
    from durin.logs.reader import LogQuery

    def first(name: str) -> str | None:
        vals = query.get(name)
        return vals[0] if vals else None

    source = (first("source") or "gateway").strip()
    filters: dict[str, set[str]] = {}
    for key in ("level", "channel", "session", "type"):
        vals = query.get(key)
        if vals:
            # repeated params OR comma-joined both supported
            collected: set[str] = set()
            for v in vals:
                collected.update(part for part in v.split(",") if part)
            if collected:
                filters[key] = collected
    before_ts = first("before_ts")
    window = first("window_hours")
    limit = first("limit")
    return LogQuery(
        source="telemetry" if source == "telemetry" else "gateway",
        q=(first("q") or None),
        before_ts=float(before_ts) if before_ts else None,
        window_hours=(None if window == "all" else float(window) if window else 24.0),
        limit=max(1, min(int(limit), 1000)) if limit else 200,
        filters=filters,
    )
```

- [ ] **Step 3b: Add the handler method on the channel class**

```python
    def _handle_logs_list(self, request: WsRequest) -> Response:
        """`GET /api/logs?source=&...` — read-only log viewer (gateway/telemetry)."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from pathlib import Path

        from durin.cli.gateway_daemon import daemon_logs_path
        from durin.logs.reader import compute_facets, read_page

        query = _parse_query(request.path)
        log_query = _logs_query_from_params(query)
        if log_query.source == "telemetry":
            directory = Path.home() / ".cache" / "durin" / "telemetry"
        else:
            directory = daemon_logs_path().parent
        try:
            page = read_page(directory, log_query)
            facets = compute_facets(directory, log_query.source)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"log read failed: {exc}")
        return _http_json_response({
            "lines": page.lines,
            "facets": facets,
            "next_cursor": page.next_cursor,
            "scanned_through_ts": page.scanned_through_ts,
            "has_more": page.has_more,
        })
```

- [ ] **Step 3c: Register the route** in `_dispatch_http` (after the `/api/config/set` block ~line 675):

```python
        if got == "/api/logs":
            return self._handle_logs_list(request)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/channels/test_logs_endpoint.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add durin/channels/websocket.py tests/channels/test_logs_endpoint.py
git commit -m "feat(api): GET /api/logs read-only viewer endpoint"
```

---

## Phase 5 — Web UI

### Task 8: `fetchLogs` API client

**Files:**
- Modify: `webui/src/lib/api.ts` (after `getModelCapabilities` ~line 470)

- [ ] **Step 1: Add types + function**

```typescript
// webui/src/lib/api.ts
export interface LogLineRow {
  ts: number;
  fields: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export interface LogFacets {
  levels?: string[];
  channels?: string[];
  sessions?: string[];
  types?: string[];
}

export interface LogPage {
  lines: LogLineRow[];
  facets: LogFacets;
  next_cursor: number | null;
  scanned_through_ts: number | null;
  has_more: boolean;
}

export interface LogQueryParams {
  source: "gateway" | "telemetry";
  q?: string;
  level?: string[];
  channel?: string[];
  session?: string[];
  type?: string[];
  beforeTs?: number | null;
  windowHours?: number | "all";
  limit?: number;
}

export async function fetchLogs(
  token: string,
  params: LogQueryParams,
  base: string = "",
): Promise<LogPage> {
  const sp = new URLSearchParams();
  sp.set("source", params.source);
  if (params.q) sp.set("q", params.q);
  for (const key of ["level", "channel", "session", "type"] as const) {
    const vals = params[key];
    if (vals && vals.length) sp.set(key, vals.join(","));
  }
  if (params.beforeTs != null) sp.set("before_ts", String(params.beforeTs));
  if (params.windowHours != null) sp.set("window_hours", String(params.windowHours));
  if (params.limit != null) sp.set("limit", String(params.limit));
  return request<LogPage>(`${base}/api/logs?${sp.toString()}`, token);
}
```

- [ ] **Step 2: Type-check**

Run: `cd webui && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add webui/src/lib/api.ts
git commit -m "feat(webui): fetchLogs API client"
```

### Task 9: `LogsSettings` component (two tabs, filters, cursor paging, widen)

**Files:**
- Create: `webui/src/components/settings/LogsSettings.tsx`

- [ ] **Step 1: Write the component**

```tsx
// webui/src/components/settings/LogsSettings.tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCw, ScrollText } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  ApiError,
  fetchLogs,
  setConfigValue,
  type LogLineRow,
  type LogFacets,
  type LogQueryParams,
} from "@/lib/api";
import { SettingsSectionTitle } from "./primitives";

type Tab = "gateway" | "telemetry";

export function LogsSettings({ token }: { token: string }) {
  const [tab, setTab] = useState<Tab>("gateway");
  return (
    <div className="space-y-6">
      <SettingsSectionTitle>
        <span className="flex items-center gap-2">
          <ScrollText className="h-4 w-4" aria-hidden /> Logs
        </span>
      </SettingsSectionTitle>
      <div className="flex gap-2">
        {(["gateway", "telemetry"] as Tab[]).map((k) => (
          <Button
            key={k}
            size="sm"
            variant={tab === k ? "default" : "ghost"}
            className="rounded-full capitalize"
            onClick={() => setTab(k)}
          >
            {k}
          </Button>
        ))}
      </div>
      <LogTable key={tab} token={token} source={tab} />
    </div>
  );
}

function LogTable({ token, source }: { token: string; source: Tab }) {
  const [rows, setRows] = useState<LogLineRow[]>([]);
  const [facets, setFacets] = useState<LogFacets>({});
  const [cursor, setCursor] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [windowHours, setWindowHours] = useState<number | "all">(24);
  const [q, setQ] = useState("");
  const [level, setLevel] = useState<string[]>([]);
  const [channel, setChannel] = useState<string[]>([]);
  const [session, setSession] = useState<string[]>([]);
  const [type, setType] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);

  const baseParams = useMemo<LogQueryParams>(() => ({
    source,
    q: q.trim() || undefined,
    level: source === "gateway" ? level : undefined,
    channel: source === "gateway" ? channel : undefined,
    session: source === "telemetry" ? session : undefined,
    type: source === "telemetry" ? type : undefined,
    windowHours,
    limit: 200,
  }), [source, q, level, channel, session, type, windowHours]);

  const load = useCallback(async (append: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const page = await fetchLogs(token, {
        ...baseParams,
        beforeTs: append ? cursor : null,
      });
      setRows((prev) => (append ? [...prev, ...page.lines] : page.lines));
      setFacets(page.facets);
      setCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token, baseParams, cursor]);

  // Reload from scratch whenever filters change.
  useEffect(() => { void load(false); /* eslint-disable-next-line */ }, [baseParams]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search…"
          className="h-8 flex-1 min-w-[180px] rounded-md border bg-background px-2 text-[13px]"
        />
        {source === "gateway" ? (
          <MultiSelect label="level" options={facets.levels ?? []} value={level} onChange={setLevel} />
        ) : (
          <MultiSelect label="type" options={facets.types ?? []} value={type} onChange={setType} />
        )}
        {source === "gateway" ? (
          <MultiSelect label="channel" options={facets.channels ?? []} value={channel} onChange={setChannel} />
        ) : (
          <MultiSelect label="session" options={facets.sessions ?? []} value={session} onChange={setSession} />
        )}
        <Button size="sm" variant="ghost" onClick={() => void load(false)} disabled={loading} className="rounded-full">
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>

      {source === "gateway" ? <GatewayConfigRow token={token} /> : null}

      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

      <div className="max-h-[60vh] overflow-auto rounded-md border font-mono text-[12px]">
        {rows.map((r, i) => (
          <div key={i} className="border-b last:border-0">
            <button
              className="flex w-full gap-3 px-2 py-1 text-left hover:bg-muted/50"
              onClick={() => setExpanded(expanded === i ? null : i)}
            >
              <span className="tabular-nums text-muted-foreground">
                {new Date(r.ts * 1000).toLocaleString()}
              </span>
              {source === "gateway" ? (
                <>
                  <span className="w-14 shrink-0">{String(r.fields.level ?? "")}</span>
                  <span className="w-24 shrink-0 text-muted-foreground">{String(r.fields.channel ?? "")}</span>
                  <span className="truncate">{String(r.fields.message ?? "")}</span>
                </>
              ) : (
                <>
                  <span className="w-32 shrink-0 text-muted-foreground">{String(r.fields.session ?? "")}</span>
                  <span className="w-48 shrink-0">{String(r.fields.type ?? "")}</span>
                  <span className="truncate">{JSON.stringify(r.fields.data ?? {})}</span>
                </>
              )}
            </button>
            {expanded === i ? (
              <pre className="overflow-auto bg-muted/40 px-3 py-2 text-[11px]">
                {JSON.stringify(r.raw, null, 2)}
              </pre>
            ) : null}
          </div>
        ))}
        {rows.length === 0 && !loading ? (
          <p className="px-2 py-4 text-muted-foreground">No log lines in this window.</p>
        ) : null}
      </div>

      {hasMore ? (
        <div className="flex items-center gap-3">
          <Button size="sm" variant="ghost" className="rounded-full" disabled={loading}
            onClick={() => void load(true)}>
            Load older
          </Button>
          {windowHours !== "all" ? (
            <button className="text-[12px] text-muted-foreground underline"
              onClick={() => setWindowHours((w) => (w === "all" ? "all" : w === 24 ? 168 : "all"))}>
              Searched last {windowHours}h — widen
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function GatewayConfigRow({ token }: { token: string }) {
  const [mb, setMb] = useState("");
  const [days, setDays] = useState("");
  const [saved, setSaved] = useState(false);
  const save = async (key: string, value: number) => {
    await setConfigValue(token, key, value);
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border bg-muted/30 px-2 py-1 text-[12px]">
      <span className="text-muted-foreground">Rotation</span>
      <input value={mb} onChange={(e) => setMb(e.target.value)} placeholder="max MB"
        className="h-7 w-20 rounded border bg-background px-1" />
      <Button size="sm" variant="ghost" className="rounded-full"
        disabled={!mb} onClick={() => void save("logging.max_file_mb", Number(mb))}>set</Button>
      <span className="text-muted-foreground">Retention</span>
      <input value={days} onChange={(e) => setDays(e.target.value)} placeholder="days"
        className="h-7 w-20 rounded border bg-background px-1" />
      <Button size="sm" variant="ghost" className="rounded-full"
        disabled={!days} onClick={() => void save("logging.retention_days", Number(days))}>set</Button>
      {saved ? <span className="text-emerald-600">saved</span> : null}
    </div>
  );
}

function MultiSelect({
  label, options, value, onChange,
}: { label: string; options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  const toggle = (opt: string) =>
    onChange(value.includes(opt) ? value.filter((v) => v !== opt) : [...value, opt]);
  return (
    <details className="relative">
      <summary className="h-8 cursor-pointer list-none rounded-md border bg-background px-2 text-[12px] leading-8">
        {label}{value.length ? ` (${value.length})` : ""}
      </summary>
      <div className="absolute z-10 mt-1 max-h-60 w-48 overflow-auto rounded-md border bg-popover p-1 shadow">
        {options.map((opt) => (
          <label key={opt} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-[12px] hover:bg-muted">
            <input type="checkbox" checked={value.includes(opt)} onChange={() => toggle(opt)} />
            <span className="truncate">{opt}</span>
          </label>
        ))}
        {options.length === 0 ? <span className="px-1 text-[11px] text-muted-foreground">—</span> : null}
      </div>
    </details>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd webui && npx tsc --noEmit`
Expected: no new errors. (If `SettingsSectionTitle` rejects a non-string child, wrap the icon+text in the existing pattern used by other sections, or pass a plain string "Logs".)

- [ ] **Step 3: Commit**

```bash
git add webui/src/components/settings/LogsSettings.tsx
git commit -m "feat(webui): LogsSettings two-tab read-only viewer"
```

### Task 10: Wire `logs` into `SettingsView`

**Files:**
- Modify: `webui/src/components/settings/SettingsView.tsx` (import ~line 65; `SettingsSectionKey` ~line 79-87; dispatch ~line 411-421; `SETTINGS_NAV_ITEMS` ~line 465)

- [ ] **Step 1: Add import** (near the CronSettings import ~line 65):

```typescript
import { LogsSettings } from "@/components/settings/LogsSettings";
import { ScrollText } from "lucide-react";
```
(If lucide icons are imported as a group elsewhere, add `ScrollText` to that import instead of a new line.)

- [ ] **Step 2: Extend the union** (~line 87, add before `;`):

```typescript
  | "advanced"
  | "logs";
```

- [ ] **Step 3: Add the dispatch branch** (in the `activeSection === ...` chain ~line 411, add a branch, e.g. after `cron`):

```tsx
              ) : activeSection === "logs" ? (
                <LogsSettings token={token} />
```

- [ ] **Step 4: Add the nav item** to `SETTINGS_NAV_ITEMS` (~line 465). Match the existing item shape (it maps `{ key, icon }`); add:

```typescript
  { key: "logs", icon: ScrollText },
```
(If items carry a label/i18n key, follow that shape — open the array literal and copy a sibling's structure, substituting `logs` / `ScrollText`.)

- [ ] **Step 5: Type-check + build**

Run: `cd webui && npx tsc --noEmit && npm run build`
Expected: builds clean.

- [ ] **Step 6: Commit**

```bash
git add webui/src/components/settings/SettingsView.tsx
git commit -m "feat(webui): add Logs section to settings navigation"
```

---

## Phase 6 — Docs + live verification

### Task 11: Update ARCHITECTURE.md

**Files:**
- Modify: `docs/ARCHITECTURE.md`

- [ ] **Step 1:** Add a short subsection under the channels / telemetry area describing: gateway log is now JSONL (loguru `serialize=True`) with size rotation + gz + `logging.*` retention; `durin/logs/reader.py` is the shared read primitive (newest-first, cursor, window, gz, grep-before-parse); `/api/logs` serves the read-only viewer; telemetry backend is unchanged.

- [ ] **Step 2: Commit**

```bash
git add docs/ARCHITECTURE.md
git commit -m "docs: gateway JSONL log lifecycle + logs read primitive"
```

### Task 12: Full backend test sweep

- [ ] **Step 1:** Run the new suites under CI-like env:

```bash
HOME=/tmp/durin-empty GIT_CONFIG_NOSYSTEM=1 \
  pytest tests/config/test_logging_config.py tests/cli/test_gateway_logging.py \
  tests/cli/test_gateway_daemon_logging.py tests/logs/ tests/channels/test_logs_endpoint.py -v
```
Expected: all PASS. Root-cause any failure (do not skip).

### Task 13: Live verification (real gateway + webui)

- [ ] **Step 1:** Start the gateway as a daemon and confirm the file is JSONL:

```bash
durin gateway --daemon
sleep 3
head -n 1 ~/.durin/logs/gateway.log | python -c "import sys, json; json.loads(sys.stdin.readline()); print('JSONL OK')"
```
Expected: prints `JSONL OK`. Also confirm `~/.durin/logs/gateway.boot.log` exists.

- [ ] **Step 2:** Open the dashboard → Settings → Logs. Verify:
  - Gateway tab lists lines newest-first; level + channel filters populate; search narrows; "Load older" pages; editing rotation/retention persists (re-open shows it stuck via `durin config get logging.max_file_mb`).
  - Telemetry tab lists events; session + type filters populate from filenames/registry; expanding a row shows the JSON `data`; a `.jsonl.gz` (if present) is read without error.

- [ ] **Step 3:** Use the `verify` skill to confirm behavior against the running app, then stop the daemon: `durin gateway stop`.

---

## Self-Review notes

- **Spec coverage:** Config (T1) · gateway JSONL sink + rotation/gz/retention (T2) · gating + boot.log + excepthook (T3) · parsing (T4) · newest-first/cursor/window/gz/grep reader (T5) · cheap facets (T6) · `/api/logs` (T7) · client (T8) · two-tab UI + widen + config knobs (T9-T10) · docs (T11) · CI-env + live gates (T12-T13). Telemetry backend untouched throughout.
- **Naming consistency:** `read_page`, `compute_facets`, `segment_files`, `parse_line`, `open_text`, `session_from_filename`, `LogQuery`, `LogPage`, `LogLine`, `_logs_query_from_params`, `configure_gateway_file_logging`, `install_excepthook`, `daemon_boot_logs_path`, `GATEWAY_LOG_FILE_ENV`, `fetchLogs`, `LogsSettings` — used identically across tasks.
- **Known integration unknowns to resolve in-task (not placeholders):** (a) exact `SETTINGS_NAV_ITEMS` item shape and whether labels are i18n keys — copy a sibling; (b) loguru's exact rotated-segment filename for the gz glob — verify in T2 and adjust the test glob; (c) the precise gateway callback in `commands.py` to host the sink block — search `@gateway_app.callback`.
