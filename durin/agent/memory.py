"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
import json
import os
import re
import weakref
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

import tiktoken
from loguru import logger

from durin.memory.consolidator_tags import parse_consolidator_response
from durin.session.manager import Session
from durin.telemetry.logger import current_telemetry
from durin.utils.gitstore import GitStore
from durin.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    find_legal_message_start,
    strip_think,
    truncate_text,
)
from durin.utils.post_compaction_guard import PostCompactionLoopGuard
from durin.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from durin.providers.base import LLMProvider
    from durin.session.manager import SessionManager


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for memory files: history.jsonl, SOUL.md."""

    _DEFAULT_MAX_HISTORY = 1000
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._corruption_logged = False  # rate-limit non-int cursor warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "memory/.dream_cursor",
        ])
        self._maybe_migrate_legacy_history()

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(self, entry: str, *, max_chars: int | None = None) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone by history replay / consolidation downstream.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net: individual callers should cap their own
        content more tightly; this default only exists to catch unintentional
        large writes (e.g. an LLM echoing its input back as a "summary").
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        if len(raw) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit, len(raw),
                )
            raw = truncate_text(raw, limit)
        content = strip_think(raw)
        if raw and not content:
            logger.debug(
                "history entry {} stripped to empty (likely template leak); "
                "persisting empty content to avoid re-polluting context",
                cursor,
            )
        record = {"cursor": cursor, "timestamp": ts, "content": content}
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Int cursors only — reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for entries with int cursors; warn once on corruption."""
        poisoned: Any = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains a non-int cursor ({!r}); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return the next value."""
        if self._cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
        # Fast path: trust the tail when intact.  Otherwise scan the whole
        # file and take ``max`` — that stays correct even if the monotonic
        # invariant was broken by external writes.
        last = self._read_last_entry() or {}
        cursor = self._valid_cursor(last.get("cursor"))
        if cursor is not None:
            return cursor + 1
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)

            # fsync the directory so the rename is durable.
            # On Windows, opening a directory with O_RDONLY raises
            # PermissionError — skip the dir sync there (NTFS
            # journals metadata synchronously).
            with suppress(PermissionError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(self, messages: list[dict], *, max_chars: int | None = None) -> None:
        """Fallback: dump raw messages to history.jsonl without LLM summarization."""
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        formatted = truncate_text(self._format_messages(messages), limit)
        self.append_history(
            f"[RAW] {len(messages)} messages\n"
            f"{formatted}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )



# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------


# Individual history.jsonl writers cap their own payloads tightly; the
# _HISTORY_ENTRY_HARD_CAP at append_history() is a belt-and-suspenders default
# that catches any new caller that forgot to set its own cap.
_RAW_ARCHIVE_MAX_CHARS = 16_000       # fallback dump (LLM failed)
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000    # LLM-produced consolidation summary
_HISTORY_ENTRY_HARD_CAP = 64_000      # emergency cap in append_history

# P5 (2026-06-10): memory refs surfaced by memory tools in the evicted
# span. The section markers are single-sourced in
# `durin.memory.section_markers`; SKILL is excluded (procedures, not
# located facts). Tool results are json-encoded in session messages, so
# the pattern is not line-anchored.
_CITED_REF_MARKER_RE = re.compile(
    r"=== (?:CANONICAL|FRAGMENT|SESSION|INGESTED): (.+?) ==="
)
_CITED_REF_TOOLS = frozenset({"memory_search", "memory_drill"})
_MAX_CITED_REFS = 20


def extract_cited_memory_refs(messages: list[dict[str, Any]]) -> list[str]:
    """Distinct memory refs surfaced by memory tools in *messages*.

    Consolidation summarizes evicted turns away — including search
    results the model fetched on purpose. The refs (not the content)
    survive into the summary so the model keeps "I know where it is"
    at pointer cost. Mechanical extraction, no LLM trust: scans tool
    messages from memory_search / memory_drill for section markers and
    returns refs first-seen-ordered, deduped, capped.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        if message.get("name") not in _CITED_REF_TOOLS:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for match in _CITED_REF_MARKER_RE.finditer(content):
            ref = match.group(1).rsplit(" (", 1)[0].strip()
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
                if len(refs) >= _MAX_CITED_REFS:
                    return refs
    return refs


# Discovered-path preservation (2026-07-17): file/dir paths surfaced by
# discovery tools in the evicted span ride the session summary
# mechanically, mirroring the cited-refs trailer above — the LLM
# summarizer cannot drop them. history.jsonl stays untouched.
_PATH_DISCOVERY_TOOLS = frozenset({
    "read_file", "write_file", "edit_file", "exec", "grep", "list_dir",
})
_MAX_DISCOVERED_PATHS = 15
# The lookbehind class deliberately excludes ':' and '/' so scheme URLs
# ("https://…") never match — the char before their first '/' is ':',
# before the second it is '/'. The relative arm requires an alphabetic
# extension so version tokens ("durin-agent/0.2.0") don't qualify.
_DISCOVERED_PATH_RE = re.compile(
    r"(?:(?<=[\s\"'`(\[=,])|^)"
    r"((?:/|~/)[\w.\-+@/]{3,}"                          # absolute or ~/ path
    r"|[\w.\-+@]+(?:/[\w.\-+@]+)+\.[A-Za-z]\w{0,7})",   # relative path w/ alpha extension
    re.MULTILINE,
)


def extract_discovered_paths(messages: list[dict[str, Any]]) -> list[str]:
    """Distinct filesystem paths surfaced by discovery tools in *messages*.

    Two sources, in first-seen order: path-bearing string arguments of
    assistant tool_calls to discovery tools (deliberate targets), then
    path-like tokens inside discovery-tool results. Deduped and capped
    so a single `find` dump cannot flood the summary trailer.
    """
    seen: dict[str, None] = {}

    def _harvest(text: str) -> None:
        for match in _DISCOVERED_PATH_RE.finditer(text):
            seen.setdefault(match.group(1).rstrip(".,;:/"), None)

    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") not in _PATH_DISCOVERY_TOOLS:
                    continue
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(args, dict):
                    for value in args.values():
                        if isinstance(value, str):
                            _harvest(value)
                        elif isinstance(value, list):
                            for item in value:
                                if isinstance(item, str):
                                    _harvest(item)
        elif role == "tool" and message.get("name") in _PATH_DISCOVERY_TOOLS:
            content = message.get("content")
            if isinstance(content, str):
                _harvest(content)

    return list(seen)[:_MAX_DISCOVERED_PATHS]


class Consolidator:
    """Lightweight consolidation: summarizes evicted messages into history.jsonl."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    # Tier 2 A3: aggregate timeout for acquiring the per-session compaction
    # lock. If a prior compaction hung (e.g. provider call stuck
    # mid-summarize), waiting on the lock indefinitely starves the session
    # lane and the next user message just hangs. After this many seconds,
    # the acquisition is abandoned and the caller proceeds without
    # consolidating — an oversized prompt is recoverable, a hung session is
    # not. Override with ``DURIN_COMPACTION_LOCK_TIMEOUT_S``.
    _DEFAULT_LOCK_TIMEOUT_S = 180.0

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        consolidation_ratio: float = 0.5,
        preemptive_compact_ratio: float = 0.5,
        decision_log_enabled: bool = True,
        decision_log_max_entries: int = 10,
        decision_log_max_chars: int = 1500,
        compaction_learnings_enabled: bool = True,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        # ``consolidation_ratio`` now means: after a compaction round, how
        # much of the *trigger threshold* should remain (default 0.5 → leave
        # half of the trigger). Pre-emptive compaction raised the trigger from
        # "near the context wall" to a much earlier point, so
        # keeping the old "fraction of budget" semantic would compact almost
        # nothing per round.
        self.consolidation_ratio = consolidation_ratio
        # Pre-emptive compaction trigger ratio.
        # Fraction of ``context_window_tokens`` above which a turn forces
        # consolidation BEFORE the LLM call (instead of waiting for a 400
        # from context-overflow). Per-model: a 128K-window model wants ~0.5;
        # a 1M-window model wants ~0.15 (you pay for every token shipped, so
        # waiting until 500K means shipping a huge prompt every turn). Set
        # in ``ModelPresetConfig.preemptive_compact_ratio`` for per-preset
        # overrides; otherwise inherits from ``AgentDefaults``.
        self.preemptive_compact_ratio = preemptive_compact_ratio
        # Concern B (task-state anchor): caps + toggle for the auto-extracted
        # decision log written at compaction. See durin/session/decision_log.py.
        self.decision_log_enabled = decision_log_enabled
        self.decision_log_max_entries = decision_log_max_entries
        self.decision_log_max_chars = decision_log_max_chars
        # Toggle for the compaction backstop that extracts durable user learnings
        # (preferences, corrections, standing constraints) at compaction time.
        self.compaction_learnings_enabled = compaction_learnings_enabled
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # Tier 2 C2: post-compaction loop guard. The consolidator arms
        # this per-session after each successful compaction round; the
        # runner observes tool calls within the window. Shared instance
        # so all sessions through one Consolidator share the same window
        # size config without each AgentRunSpec carrying its own.
        self.post_compaction_guard = PostCompactionLoopGuard()
        # Optional callback fired once per
        # ``maybe_consolidate_by_tokens`` call that produced at least
        # one summary. Wiring layer (``cli/commands.py``) sets this to
        # a thunk that dispatches a background reactive dream (extract) pass so
        # consolidated context turns into entity-page updates while the
        # signal is fresh. Sync callable — must not block; the
        # callback owns its own threading if needed.
        self.on_post_compaction: Callable[[str], None] | None = None

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
        *,
        preemptive_compact_ratio: float | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = provider.generation.max_tokens
        # Per-preset ratio override (Tier 2 A1). When the model preset
        # changes (set_model_preset → _apply_provider_snapshot), callers
        # can supply the preset's preemptive_compact_ratio. None leaves
        # the existing ratio untouched.
        if preemptive_compact_ratio is not None:
            self.preemptive_compact_ratio = preemptive_compact_ratio

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    @classmethod
    def _lock_timeout_s(cls) -> float:
        """Resolve the compaction-lock aggregate timeout (Tier 2 A3).

        Reads ``DURIN_COMPACTION_LOCK_TIMEOUT_S`` if set, else falls back
        to ``_DEFAULT_LOCK_TIMEOUT_S``. ``0`` or negative disables the
        timeout (revert to legacy unbounded wait).
        """
        raw = os.getenv("DURIN_COMPACTION_LOCK_TIMEOUT_S")
        if raw is None:
            return cls._DEFAULT_LOCK_TIMEOUT_S
        try:
            value = float(raw)
        except ValueError:
            return cls._DEFAULT_LOCK_TIMEOUT_S
        return value  # negative/0 → handled at use site as "unbounded"

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    @staticmethod
    def _full_unconsolidated_history(
        session: Session,
        *,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the whole unconsolidated tail for consolidation decisions."""
        unconsolidated_count = len(session.messages) - session.last_consolidated
        if unconsolidated_count <= 0:
            return []
        return session.get_history(
            max_messages=unconsolidated_count,
            include_timestamps=include_timestamps,
        )

    @staticmethod
    def _replay_overflow_boundary(
        session: Session,
        replay_max_messages: int | None,
    ) -> int | None:
        if not replay_max_messages or replay_max_messages <= 0:
            return None
        tail = list(enumerate(session.messages[session.last_consolidated:], session.last_consolidated))
        if len(tail) <= replay_max_messages:
            return None

        sliced = tail[-replay_max_messages:]
        for i, (_idx, message) in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1][1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        legal_start = find_legal_message_start([message for _idx, message in sliced])
        if legal_start:
            sliced = sliced[legal_start:]
        if not sliced:
            return len(session.messages)

        first_visible_idx = sliced[0][0]
        if first_visible_idx <= session.last_consolidated:
            return None
        return first_visible_idx

    async def _consolidate_replay_overflow(
        self,
        session: Session,
        replay_max_messages: int | None,
    ) -> str | None:
        """Archive messages that would be hidden by the replay message window."""
        end_idx = self._replay_overflow_boundary(session, replay_max_messages)
        if end_idx is None:
            return None
        chunk = session.messages[session.last_consolidated:end_idx]
        if not chunk:
            return None
        logger.info(
            "Replay-window consolidation for {}: chunk={} msgs, replay_max={}",
            session.key,
            len(chunk),
            replay_max_messages,
        )
        summary, tags = await self.archive(chunk)
        self._merge_session_tags(session, tags)
        session.last_consolidated = end_idx
        self.sessions.save(session)
        return summary

    @staticmethod
    def _merge_session_tags(
        session: Session,
        new_tags: dict[str, list[str]] | None,
    ) -> None:
        """Merge entity/topic tags into ``session.metadata['_last_tags']``.

        Tags accumulate via set union across compactions within a single
        session — Phase 3 dream is responsible for later pruning.
        """
        new_entities = (new_tags or {}).get("entities") or []
        new_topics = (new_tags or {}).get("topics") or []
        if not new_entities and not new_topics:
            return
        existing = session.metadata.get("_last_tags", {})
        if not isinstance(existing, dict):
            existing = {}
        existing_entities = existing.get("entities") or []
        existing_topics = existing.get("topics") or []
        session.metadata["_last_tags"] = {
            "entities": sorted(set(existing_entities) | set(new_entities)),
            "topics": sorted(set(existing_topics) | set(new_topics)),
        }

    def _persist_last_summary(
        self, session: Session, summaries: list[str],
    ) -> None:
        """Append this call's span summaries to the session-summary projection.

        One block per archived span; the store enforces the total char cap
        (oldest blocks evicted first). The projection under
        ``memory/session_summary/<key>.md`` remains the single source of
        truth; legacy ``_last_summary`` metadata is still dropped on sight.
        """
        from durin.memory.session_summary_store import (
            append_session_summary_block,
        )
        for summary in summaries:
            if not summary or summary == "(nothing)":
                continue
            try:
                append_session_summary_block(
                    self.store.workspace,
                    session.key,
                    summary,
                    last_active=session.updated_at,
                )
            except Exception as exc:  # noqa: BLE001
                # The persistence must NEVER break the compaction
                # path — the summary will be regenerated next time
                # if this write fails. Log loudly so the failure
                # surfaces in operator review.
                logger.warning(
                    "session_summary: append for {} failed: {}",
                    session.key, exc,
                )
        # Drop the legacy field from metadata if it was carrying a
        # pre-A10 summary. Saves the session so the JSON reflects
        # the new state (single source of truth principle).
        legacy = session.metadata.pop("_last_summary", None)
        if legacy is not None:
            try:
                self.sessions.save(session)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "session_summary: failed to drop legacy "
                    "metadata for {}: {}", session.key, exc,
                )

    def estimate_session_prompt_tokens(
        self,
        session: Session,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full unconsolidated session tail."""
        history = self._full_unconsolidated_history(session, include_timestamps=True)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        # Include archived summary in estimation so the budget accounts for it.
        # A10: summary lives in `memory/session_summary/<key>.md` now (single
        # source of truth); the legacy `session.metadata["_last_summary"]` is
        # kept as a backward-compat fallback for pre-A10 sessions until they
        # next compact (at which point `_persist_last_summary` migrates).
        from durin.memory.session_summary_store import get_session_summary
        summary, _ = get_session_summary(self.store.workspace, session.key)
        if summary is None:
            legacy = session.metadata.get("_last_summary")
            if isinstance(legacy, dict):
                summary = legacy.get("text") if isinstance(legacy.get("text"), str) else None
            elif isinstance(legacy, str):
                summary = legacy
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            sender_id=None,
            session_summary=summary,
            session_metadata=session.metadata,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @property
    def _input_token_budget(self) -> int:
        """Available input token budget for consolidation LLM."""
        return self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER

    @property
    def _preemptive_trigger_tokens(self) -> int:
        """Token count at which a turn forces consolidation before the
        LLM call.

        Bounded above by the legacy ``_input_token_budget`` so a misconfigured
        ratio (e.g. 0.99) can't disable the hard ceiling — context overflow
        still triggers even if the ratio would have skipped.
        """
        if self.context_window_tokens <= 0:
            return 0
        ratio = self.preemptive_compact_ratio
        if not isinstance(ratio, (int, float)) or ratio <= 0:
            # 0 / negative / garbage → fall back to legacy behavior (trigger
            # only at hard budget).
            return self._input_token_budget
        threshold = int(self.context_window_tokens * float(ratio))
        return max(1, min(threshold, self._input_token_budget))

    def _truncate_to_token_budget(self, text: str) -> str:
        """Truncate text so it fits within the consolidation LLM's token budget."""
        budget = self._input_token_budget
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= budget:
                return text
            return enc.decode(tokens[:budget]) + "\n... (truncated)"
        except Exception:
            return truncate_text(text, budget * 4)

    async def archive(
        self, messages: list[dict]
    ) -> tuple[str | None, dict[str, list[str]]]:
        """Summarize messages via LLM and append to history.jsonl.

        Returns ``(summary, tags)``. ``summary`` is the bullet-list portion
        of the LLM response (with the trailing tags YAML block stripped),
        or ``None`` on empty input or LLM failure. ``tags`` is
        ``{"entities": [...], "topics": [...]}`` parsed from the trailing
        YAML block, or both empty lists when the response lacks tags or
        parsing fails (degraded LLM output must never crash compaction).
        """
        empty_tags: dict[str, list[str]] = {"entities": [], "topics": []}
        if not messages:
            return None, empty_tags
        try:
            formatted = MemoryStore._format_messages(messages)
            formatted = self._truncate_to_token_budget(formatted)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_archive.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            raw = response.content or "[no summary]"
            self.store.append_history(raw, max_chars=_ARCHIVE_SUMMARY_MAX_CHARS)
            summary, tags = parse_consolidator_response(raw)
            # P5 (2026-06-10): the refs ride the session summary only —
            # history.jsonl (dream input) stays untouched. Mechanical,
            # appended after the LLM output so it can't be dropped or
            # hallucinated by the summarizer.
            cited = extract_cited_memory_refs(messages)
            if summary and cited:
                summary = (
                    f"{summary}\n"
                    "Memory refs cited in this span "
                    "(memory_drill for full bodies): "
                    + "; ".join(cited)
                )
            discovered = extract_discovered_paths(messages)
            if summary and discovered:
                summary = (
                    f"{summary}\n"
                    "Files/paths examined in this span "
                    "(read_file to reopen): " + "; ".join(discovered)
                )
                _t = current_telemetry()
                if _t is not None:
                    with suppress(Exception):
                        _t.log("compaction.paths_preserved", {
                            "count": len(discovered),
                        })
            return summary, tags
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return None, empty_tags

    async def extract_decisions(self, messages: list[dict]) -> list[str]:
        """Extract key task decisions/findings from a span via LLM (best-effort).

        Concern B (task-state anchor): runs once per compaction over the span
        just archived, so the model keeps *why* it did things even after the
        raw messages leave the window. Returns a flat list of one-line
        decisions, or [] on empty input, "(none)", or any LLM failure
        (degraded output must never crash compaction).
        """
        if not messages:
            return []
        try:
            formatted = MemoryStore._format_messages(messages)
            formatted = self._truncate_to_token_budget(formatted)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_decisions.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            if response.finish_reason == "error":
                return []
            raw = (response.content or "").strip()
            if not raw or raw.lower() == "(none)":
                return []
            out: list[str] = []
            for line in raw.splitlines():
                cleaned = line.strip().lstrip("-*•").strip()
                if cleaned and cleaned.lower() != "(none)":
                    out.append(cleaned[:400])
            return out
        except Exception:
            logger.warning("Decision extraction LLM call failed; skipping")
            return []

    async def extract_learnings(
        self, messages: list[dict]
    ) -> list[dict[str, str]]:
        """Extract durable user learnings from a span via LLM (best-effort).

        Mirrors extract_decisions but targets feedback/stance/practice/person
        entities: preferences, corrections, standing constraints, and stable
        personal facts about the user. Returns a list of
        {"ref": str, "name": str, "body": str} objects, or [] on empty input,
        empty LLM output, or any failure (must never crash compaction).
        """
        if not messages:
            return []
        try:
            from json_repair import repair_json

            formatted = MemoryStore._format_messages(messages)
            formatted = self._truncate_to_token_budget(formatted)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_learnings.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            if response.finish_reason == "error":
                return []
            raw = (response.content or "").strip()
            if not raw:
                return []
            # Strip code fences if present.
            m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
            if m:
                raw = m.group(1).strip()
            try:
                obj = json.loads(repair_json(raw))
            except Exception:
                return []
            if not isinstance(obj, list):
                return []
            out: list[dict[str, str]] = []
            for item in obj:
                if not isinstance(item, dict):
                    continue
                ref = item.get("ref", "")
                name = item.get("name", "")
                body = item.get("body", "")
                if (
                    isinstance(ref, str) and ":" in ref
                    and isinstance(name, str)
                    and isinstance(body, str)
                    and ref and name and body
                ):
                    out.append({"ref": ref, "name": name, "body": body[:400]})
            return out
        except Exception:
            logger.warning("Learning extraction LLM call failed; skipping")
            return []

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
        *,
        replay_max_messages: int | None = None,
    ) -> None:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.
        """
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        # Tier 2 A3: bounded lock acquisition. A prior compaction that
        # hung mid-summarize would have left this lock held; without a
        # timeout, every subsequent maybe_consolidate_by_tokens call on
        # this session would hang forever. Abandon the acquisition after
        # ``_lock_timeout_s()`` and let the caller proceed without
        # consolidation — the prompt may be oversized but the session
        # lane is unblocked.
        lock_timeout = self._lock_timeout_s()
        try:
            if lock_timeout > 0:
                await asyncio.wait_for(lock.acquire(), timeout=lock_timeout)
            else:
                await lock.acquire()
        except asyncio.TimeoutError:
            logger.warning(
                "Compaction lock acquisition timed out after {}s for {}; "
                "skipping consolidation this turn (a prior compaction "
                "may be stuck)",
                lock_timeout,
                session.key,
            )
            _to_logger = current_telemetry()
            if _to_logger is not None:
                with suppress(Exception):
                    _to_logger.log("compaction.lock_timeout", {
                        "session_key": session.key,
                        "timeout_s": lock_timeout,
                    })
            return
        try:
            # Tier 2 A1: trigger consolidation early instead of waiting for
            # the hard budget ceiling. ``target`` is computed off the trigger
            # so each compaction round does meaningful work (compacting down
            # by ``consolidation_ratio`` of the trigger).
            trigger = self._preemptive_trigger_tokens
            target = max(1, int(trigger * self.consolidation_ratio))
            new_summaries: list[str] = []
            replay_summary = await self._consolidate_replay_overflow(
                session,
                replay_max_messages,
            )
            if replay_summary:
                new_summaries.append(replay_summary)
            try:
                estimated, source = self.estimate_session_prompt_tokens(
                    session,
                )
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                # Audit P2.4: distinguish "estimator failed" from "session
                # is genuinely empty". Both legitimately skip consolidation,
                # but they're different diagnostics — the former wants
                # investigation, the latter is the boring no-op.
                if source == "error":
                    logger.debug(
                        "Token consolidation skipped for {}: estimator raised "
                        "(check the exception traceback above); session has {} msg(s)",
                        session.key,
                        len(session.messages),
                    )
                else:
                    logger.debug(
                        "Token consolidation skipped for {}: nothing to estimate "
                        "(session has {} msg(s), last_consolidated={})",
                        session.key,
                        len(session.messages),
                        session.last_consolidated,
                    )
                self._persist_last_summary(session, new_summaries)
                return
            if estimated < trigger:
                unconsolidated_count = len(session.messages) - session.last_consolidated
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}, trigger={}, msgs={}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    trigger,
                    unconsolidated_count,
                )
                self._persist_last_summary(session, new_summaries)
                return
            # Emit a one-shot telemetry event when pre-emptive trigger fires
            # below the hard budget — visibility into how often the new
            # threshold is doing actual work vs. legacy behavior.
            if estimated < self._input_token_budget:
                _logger = current_telemetry()
                if _logger is not None:
                    with suppress(Exception):
                        _logger.log("compaction.preemptive_trigger", {
                            "session_key": session.key,
                            "estimated_tokens": estimated,
                            "trigger_tokens": trigger,
                            "budget_tokens": self._input_token_budget,
                            "context_window_tokens": self.context_window_tokens,
                            "ratio": self.preemptive_compact_ratio,
                        })

            consolidated_start = session.last_consolidated
            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                end_idx = boundary[0]

                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    break

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                summary, tags = await self.archive(chunk)
                # Advance the cursor either way: on success the chunk was
                # summarized; on failure archive() already raw-archived it as
                # a breadcrumb. Re-archiving the same chunk on the next call
                # would just emit duplicate [RAW] entries.
                if summary:
                    new_summaries.append(summary)
                self._merge_session_tags(session, tags)
                session.last_consolidated = end_idx
                self.sessions.save(session)
                if not summary:
                    # LLM is degraded — stop hammering it this call;
                    # the next invocation can retry a fresh chunk.
                    break

                try:
                    estimated, source = self.estimate_session_prompt_tokens(
                        session,
                    )
                except Exception:
                    logger.exception("Token estimation failed for {}", session.key)
                    estimated, source = 0, "error"
                if estimated <= 0:
                    break

            # Persist the last summary to session metadata so it can be injected
            # into the runtime context on the next prepare_session() call, aligning
            # the summary injection strategy with AutoCompact._archive().
            self._persist_last_summary(session, new_summaries)
            # Tier 2 C2: arm the post-compaction loop guard if at least
            # one summary was produced this call. The next ``window_size``
            # tool calls on this session will be observed; identical
            # ``(name, args, result)`` triples trip the guard.
            if new_summaries:
                self.post_compaction_guard.arm(session.key)
                # Notify the post-compaction hook so
                # the entity-centric dream can pick up freshly-archived
                # episodic context while the signal is hot. Best-effort:
                # callback failures must NOT break consolidation —
                # markdown remains the source of truth.
                if self.on_post_compaction is not None:
                    try:
                        self.on_post_compaction(session.key)
                    except Exception:
                        logger.exception(
                            "post-compaction hook raised for {}",
                            session.key,
                        )
                # Shared span for both post-compaction extraction passes below.
                # Computed once here so both gated blocks can reuse it without
                # independently slicing the same list.
                span = session.messages[consolidated_start:session.last_consolidated]
                # Concern B (task-state anchor): one LLM call per compaction
                # extracts key decisions/findings from the span just archived
                # and appends them to the decision log so they survive in
                # runtime context after the raw messages leave the window.
                # Best-effort: failures must never break consolidation.
                if self.decision_log_enabled:
                    try:
                        decisions = await self.extract_decisions(span)
                        if decisions:
                            from datetime import timezone

                            from durin.session.decision_log import add_decision

                            ts = datetime.now(timezone.utc).isoformat()
                            total_dropped = 0
                            for decision in decisions:
                                _, dropped = add_decision(
                                    session.metadata, decision, source="auto", ts=ts,
                                    max_entries=self.decision_log_max_entries,
                                    max_chars=self.decision_log_max_chars,
                                )
                                total_dropped += dropped
                            self.sessions.save(session)
                            _dec_logger = current_telemetry()
                            if _dec_logger is not None:
                                with suppress(Exception):
                                    _dec_logger.log("decision_log.extracted", {
                                        "count": len(decisions),
                                        "session_key": session.key,
                                    })
                                    if total_dropped:
                                        _dec_logger.log("decision_log.capped", {
                                            "dropped": total_dropped,
                                            "source": "auto",
                                            "session_key": session.key,
                                        })
                    except Exception:
                        logger.exception("Decision extraction failed for {}", session.key)
                # Compaction backstop: distil durable user learnings (preferences,
                # corrections, standing constraints, stable personal facts) from
                # the archived span and persist them as feedback/stance/practice
                # entities. This catches what the in-the-moment capture directive
                # may have missed. Best-effort: failures must never break consolidation.
                # Dedup is NOT done here — the dream's refine pass already dedups
                # feedback entities; we rely on it.
                if self.compaction_learnings_enabled:
                    try:
                        learnings = await self.extract_learnings(span)
                        if learnings:
                            from datetime import timezone

                            from durin.memory.field_patch import FieldPatch
                            from durin.memory.memory_writer import write_entity

                            now = datetime.now(timezone.utc)
                            session_ref = f"[[sessions/{self.sessions.safe_key(session.key)}.md]]"
                            for learning in learnings:
                                ref = learning["ref"]
                                name = learning["name"]
                                body = learning["body"]
                                # Guard: only how-to-work entity types are
                                # written by the backstop. Allowing person:
                                # refs would body_replace the user's PRINCIPAL
                                # entity — that is the live agent's job, not
                                # the backstop's.
                                ref_type = ref.split(":", 1)[0]
                                if ref_type not in {"feedback", "stance", "practice"}:
                                    logger.debug(
                                        "compaction backstop: skipping ref %r (type %r not pinnable)",
                                        ref, ref_type,
                                    )
                                    continue
                                write_entity(
                                    self.store.workspace,
                                    ref,
                                    [
                                        FieldPatch(
                                            kind="body_replace",
                                            value=body,
                                            author="agent",
                                            source_ref=session_ref,
                                            at=now,
                                        )
                                    ],
                                    create=True,
                                    name=name,
                                )
                    except Exception:
                        logger.exception("Learning extraction failed for {}", session.key)
        finally:
            lock.release()


# ---------------------------------------------------------------------------
# Dream — heavyweight cron-scheduled memory consolidation
# ---------------------------------------------------------------------------


# Single source of truth for the staleness threshold used in _annotate_with_ages
# *and* in the Phase 1 prompt template (passed as `stale_threshold_days`).
# Keep code and prompt aligned — if you bump this, the LLM's instruction string
# updates automatically.
_STALE_THRESHOLD_DAYS = 14


def _approx_tokens_for_entries(entries: list[dict[str, Any]]) -> int:
    """Tokenize the user-visible payload of history.jsonl entries.

    Used as a cheap pre-LLM gate to decide whether a Dream pass is
    worth a Phase 1 LLM call. Counts `role` + `content` + the common
    tool-call fields (anything the Phase 1 prompt would render).
    Returns 0 on tokenizer failure — safer than blocking; the gate
    just doesn't trigger.
    """
    try:
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001
        return 0
    parts: list[str] = []
    for e in entries:
        for key in ("role", "content", "name", "tool_call_id"):
            v = e.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
        tcs = e.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    if isinstance(fn, dict):
                        for k in ("name", "arguments"):
                            v = fn.get(k)
                            if isinstance(v, str) and v:
                                parts.append(v)
    if not parts:
        return 0
    try:
        return sum(len(enc.encode(p)) for p in parts)
    except Exception:  # noqa: BLE001
        return 0
