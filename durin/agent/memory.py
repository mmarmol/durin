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

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.registry import ToolRegistry
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
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

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
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._corruption_logged = False  # rate-limit non-int cursor warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/.dream_cursor",
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

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self.read_file(self.memory_file)

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content, encoding="utf-8")

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

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
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        # ``consolidation_ratio`` now means: after a compaction round, how
        # much of the *trigger threshold* should remain (default 0.5 → leave
        # half of the trigger). Pre-emptive compaction (Tier 2 A1) raised the
        # trigger from "near the context wall" to a much earlier point, so
        # keeping the old "fraction of budget" semantic would compact almost
        # nothing per round.
        self.consolidation_ratio = consolidation_ratio
        # Pre-emptive compaction trigger ratio (OpenClaw-inspired Tier 2 A1).
        # Fraction of ``context_window_tokens`` above which a turn forces
        # consolidation BEFORE the LLM call (instead of waiting for a 400
        # from context-overflow). Per-model: a 128K-window model wants ~0.5;
        # a 1M-window model wants ~0.15 (you pay for every token shipped, so
        # waiting until 500K means shipping a huge prompt every turn). Set
        # in ``ModelPresetConfig.preemptive_compact_ratio`` for per-preset
        # overrides; otherwise inherits from ``AgentDefaults``.
        self.preemptive_compact_ratio = preemptive_compact_ratio
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
        # Doc 25 §2.A.1 β.2: optional callback fired once per
        # ``maybe_consolidate_by_tokens`` call that produced at least
        # one summary. Wiring layer (``cli/commands.py``) sets this to
        # a thunk that dispatches a background DreamRunner pass so
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

    def _persist_last_summary(self, session: Session, summary: str | None) -> None:
        """Persist the session summary as the single source of truth.

        A10 (2026-05-28): the summary lives as a markdown projection
        under ``memory/session_summary/<sanitized_key>.md`` — NOT in
        ``session.metadata["_last_summary"]`` anymore. The walker /
        indexer pick it up automatically, and the search pipeline
        can return it as a hit (`class_name=session_summary`, decay
        120d per A9).

        Backward-compat: if the session's metadata still carries a
        legacy ``_last_summary`` dict (pre-A10 persistence), we drop
        it here so the JSON and the markdown can't drift apart. The
        markdown is the source of truth going forward.
        """
        if summary and summary != "(nothing)":
            from durin.memory.session_summary_store import (
                write_session_summary,
            )
            try:
                write_session_summary(
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
                    "session_summary: write for {} failed: {}",
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
        LLM call (OpenClaw-inspired Tier 2 A1).

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
            return summary, tags
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return None, empty_tags

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
            last_summary = await self._consolidate_replay_overflow(
                session,
                replay_max_messages,
            )
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
                self._persist_last_summary(session, last_summary)
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
                self._persist_last_summary(session, last_summary)
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
                    last_summary = summary
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
            self._persist_last_summary(session, last_summary)
            # Tier 2 C2: arm the post-compaction loop guard if at least
            # one summary was produced this call. The next ``window_size``
            # tool calls on this session will be observed; identical
            # ``(name, args, result)`` triples trip the guard.
            if last_summary:
                self.post_compaction_guard.arm(session.key)
                # Doc 25 §2.A.1 β.2: notify the post-compaction hook so
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


class Dream:
    """Two-phase memory processor: analyze history.jsonl, then edit files via AgentRunner.

    Phase 1 produces an analysis summary (plain LLM call).
    Phase 2 delegates to AgentRunner with read_file / edit_file tools so the
    LLM can make targeted, incremental edits instead of replacing entire files.
    """

    # Caps on prompt-bound inputs so Dream's LLM calls never exceed the model's
    # context window just because a file (or a legacy large history entry) grew
    # unexpectedly. Each file still appears in full via read_file when the agent
    # needs it in Phase 2 — these caps only bound the Phase 1/2 prompt preview.
    _MEMORY_FILE_MAX_CHARS = 32_000
    _SOUL_FILE_MAX_CHARS = 16_000
    _USER_FILE_MAX_CHARS = 16_000
    _HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        max_tool_result_chars: int = 16_000,
        annotate_line_ages: bool = True,
        min_tokens_to_run: int = 2000,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        # Kill switch for the git-blame-based per-line age annotation in Phase 1.
        # Default True keeps the #3212 behavior; set False to feed MEMORY.md raw
        # (e.g. if a specific LLM reacts poorly to the `← Nd` suffix).
        self.annotate_line_ages = annotate_line_ages
        # Pre-LLM gate: skip the whole Phase 1 LLM analysis when the
        # unprocessed history.jsonl tail tokenizes to less than this. 0 =
        # disabled (every non-empty cron tick runs the LLM, pre-fix
        # behaviour). See DreamConfig.min_tokens_to_run for the rationale.
        self.min_tokens_to_run = max(0, int(min_tokens_to_run))
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self._runner.provider = provider

    # -- tool registry -------------------------------------------------------

    def _build_tools(self) -> ToolRegistry:
        """Build a minimal tool registry for the Dream agent."""
        from durin.agent.skills import BUILTIN_SKILLS_DIR
        from durin.agent.tools.file_state import FileStates
        from durin.agent.tools.filesystem import EditFileTool, ReadFileTool

        tools = ToolRegistry()
        workspace = self.store.workspace
        # Allow reading builtin skills for reference during skill creation
        extra_read = [BUILTIN_SKILLS_DIR] if BUILTIN_SKILLS_DIR.exists() else None
        # Dream gets its own FileStates so its caches stay isolated from the
        # main loop's sessions (issue #3571).
        file_states = FileStates()
        tools.register(ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            extra_allowed_dirs=extra_read,
            file_states=file_states,
        ))
        tools.register(EditFileTool(workspace=workspace, allowed_dir=workspace, file_states=file_states))
        # E2 Part A: author skills through the sanctioned store (provenance +
        # commit + fork-on-write), not a raw file write.
        from durin.agent.tools.skill_write import SkillWriteTool
        (workspace / "skills").mkdir(parents=True, exist_ok=True)
        tools.register(SkillWriteTool(workspace=workspace))
        # §6.C: the dream sees the full hit list (raw skill_search) and asks a gated
        # per-ref tool for a seed; the gate (in skill_acquire_seed) only ever returns
        # risk-free prior art, so the autonomous dream can never use risky content.
        from durin.agent.tools.skill_acquire_seed import SkillAcquireSeedTool
        from durin.agent.tools.skill_search import SkillSearchTool
        from durin.config.loader import load_config
        try:
            _sk = load_config().skills
            _regs = list(_sk.discovery.registries)
            _allow = list(_sk.security.allowlist)
            _lim = int(_sk.discovery.search_limit)
        except Exception:  # noqa: BLE001 — never block dream startup on config
            _regs, _allow, _lim = [], [], 10
        tools.register(SkillSearchTool(
            workspace=workspace, registries=_regs, allowlist=_allow, limit=_lim))
        tools.register(SkillAcquireSeedTool(workspace=workspace, allowlist=_allow))
        return tools

    # -- skill listing --------------------------------------------------------

    def _list_existing_skills(self) -> list[str]:
        """List existing skills as 'name — description' for dedup context."""
        import re as _re

        from durin.agent.skills import BUILTIN_SKILLS_DIR

        desc_re = _re.compile(r"^description:\s*(.+)$", _re.MULTILINE | _re.IGNORECASE)
        entries: dict[str, str] = {}
        for base in (self.store.workspace / "skills", BUILTIN_SKILLS_DIR):
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                skill_md = d / "SKILL.md"
                if not skill_md.exists():
                    continue
                # Prefer workspace skills over builtin (same name)
                if d.name in entries and base == BUILTIN_SKILLS_DIR:
                    continue
                content = skill_md.read_text(encoding="utf-8")[:500]
                m = desc_re.search(content)
                desc = m.group(1).strip() if m else "(no description)"
                entries[d.name] = desc
        return [f"{name} — {desc}" for name, desc in sorted(entries.items())]

    # -- main entry ----------------------------------------------------------

    def _annotate_with_ages(self, content: str) -> str:
        """Append per-line age suffixes to MEMORY.md content.

        Each non-blank line whose age exceeds ``_STALE_THRESHOLD_DAYS`` gets a
        suffix like ``← 30d`` indicating days since last modification.
        Returns the original content unchanged if git is unavailable,
        annotate fails, or the line count doesn't match the age count
        (which can happen with an uncommitted working-tree edit — better to
        skip annotation than to tag the wrong line).
        SOUL.md and USER.md are never annotated.
        """
        file_path = "memory/MEMORY.md"
        try:
            ages = self.store.git.line_ages(file_path)
        except Exception:
            logger.debug("line_ages failed for {}", file_path)
            return content
        if not ages:
            return content

        had_trailing = content.endswith("\n")
        lines = content.splitlines()
        # If HEAD-blob line count disagrees with the working-tree content we
        # received, ages would be assigned to the wrong lines — skip entirely
        # and feed the LLM un-annotated content rather than misleading data.
        if len(lines) != len(ages):
            logger.debug(
                "line_ages length mismatch for {} (lines={}, ages={}); skipping annotation",
                file_path, len(lines), len(ages),
            )
            return content

        annotated: list[str] = []
        for line, age in zip(lines, ages):
            if not line.strip():
                annotated.append(line)
                continue
            if age.age_days > _STALE_THRESHOLD_DAYS:
                annotated.append(f"{line}  \u2190 {age.age_days}d")
            else:
                annotated.append(line)
        result = "\n".join(annotated)
        if had_trailing:
            result += "\n"
        return result

    async def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        import time as _time

        from durin.agent.skills import BUILTIN_SKILLS_DIR
        from durin.agent.tools._telemetry import emit_tool_event

        t0 = _time.perf_counter()
        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            emit_tool_event(
                "memory.dream.legacy.skipped",
                {"reason": "no_entries", "model": self.model},
            )
            return False

        # Pre-LLM gate (2026-05-31): tokenize the unprocessed history tail
        # and skip the whole Phase 1 LLM call when total tokens are under
        # the user's threshold. Cheap (~ms with tiktoken) — bounds cron
        # cost during quiet periods. Counting entry text (role + content
        # + tool fields) approximates what Phase 1 will actually feed to
        # the LLM closely enough for a gate decision; the exact LLM prompt
        # also includes MEMORY.md/SOUL.md/USER.md previews but those don't
        # grow with history volume.
        tokens = _approx_tokens_for_entries(entries)
        if self.min_tokens_to_run > 0 and tokens < self.min_tokens_to_run:
            logger.info(
                "Dream: skipped — {} tokens < threshold {} ({} entries)",
                tokens, self.min_tokens_to_run, len(entries),
            )
            emit_tool_event(
                "memory.dream.legacy.skipped",
                {
                    "reason": "below_token_threshold",
                    "tokens": tokens,
                    "threshold": self.min_tokens_to_run,
                    "entries_count": len(entries),
                    "model": self.model,
                },
            )
            return False

        batch = entries[: self.max_batch_size]
        logger.info(
            "Dream: processing {} entries (cursor {}→{}), batch={}",
            len(entries), last_cursor, batch[-1]["cursor"], len(batch),
        )
        emit_tool_event(
            "memory.dream.legacy.start",
            {
                "entries_count": len(entries),
                "batch_size": len(batch),
                "tokens": tokens,
                "model": self.model,
            },
        )

        # Build history text for LLM — cap each entry so a legacy oversized
        # record (e.g. pre-#3412 raw_archive dump) can't blow up the prompt.
        history_text = "\n".join(
            f"[{e['timestamp']}] "
            f"{truncate_text(e['content'], self._HISTORY_ENTRY_PREVIEW_MAX_CHARS)}"
            for e in batch
        )

        # Current file contents + per-line age annotations (MEMORY.md only).
        # Each file is capped in the *prompt preview* only; Phase 2 still sees
        # the full file via the read_file tool.
        current_date = datetime.now().strftime("%Y-%m-%d")
        raw_memory = self.store.read_memory() or "(empty)"
        annotated_memory = (
            self._annotate_with_ages(raw_memory)
            if self.annotate_line_ages
            else raw_memory
        )
        current_memory = truncate_text(annotated_memory, self._MEMORY_FILE_MAX_CHARS)
        current_soul = truncate_text(
            self.store.read_soul() or "(empty)", self._SOUL_FILE_MAX_CHARS,
        )
        current_user = truncate_text(
            self.store.read_user() or "(empty)", self._USER_FILE_MAX_CHARS,
        )

        file_context = (
            f"## Current Date\n{current_date}\n\n"
            f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
            f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
            f"## Current USER.md ({len(current_user)} chars)\n{current_user}"
        )

        # Compact "recently-used skills" line so Phase 2 can choose to patch
        # the `auto` skills that were actually exercised. Appended only when
        # non-empty; one line, names + read/edit counts.
        from durin.agent.skill_usage import collect_recent_skill_calls

        _used = collect_recent_skill_calls(self.store.workspace, within_hours=48)
        used_skills_block = ""
        if _used:
            _used_line = "Recently-used skills: " + ", ".join(
                f"{n} (read×{o.get('read', 0)}, edit×{o.get('edit', 0)})"
                for n, o in sorted(_used.items())
            )
            used_skills_block = f"\n\n## Recently-Used Skills\n{_used_line}"

        # Phase 1: Analyze (no skills list — dedup is Phase 2's job)
        phase1_prompt = (
            f"## Conversation History\n{history_text}\n\n{file_context}"
            f"{used_skills_block}"
        )

        phase1_prompt_tokens = 0
        phase1_completion_tokens = 0
        try:
            phase1_response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/dream_phase1.md",
                            strip=True,
                            stale_threshold_days=_STALE_THRESHOLD_DAYS,
                        ),
                    },
                    {"role": "user", "content": phase1_prompt},
                ],
                tools=None,
                tool_choice=None,
            )
            analysis = phase1_response.content or ""
            usage = getattr(phase1_response, "usage", None) or {}
            phase1_prompt_tokens = int(usage.get("prompt_tokens") or 0)
            phase1_completion_tokens = int(usage.get("completion_tokens") or 0)
            logger.debug("Dream Phase 1 analysis ({} chars): {}", len(analysis), analysis[:500])
        except Exception:
            logger.exception("Dream Phase 1 failed")
            emit_tool_event(
                "memory.dream.legacy.end",
                {
                    "status": "phase1_failed",
                    "duration_ms": int((_time.perf_counter() - t0) * 1000),
                    "cursor_advanced": False,
                    "changelog_count": 0,
                    "phase1_prompt_tokens": phase1_prompt_tokens,
                    "phase1_completion_tokens": phase1_completion_tokens,
                    "model": self.model,
                },
            )
            return False

        # Phase 2: Delegate to AgentRunner with read_file / edit_file
        existing_skills = self._list_existing_skills()
        skills_section = ""
        if existing_skills:
            skills_section = (
                "\n\n## Existing Skills\n"
                + "\n".join(f"- {s}" for s in existing_skills)
            )
        phase2_prompt = f"## Analysis Result\n{analysis}\n\n{file_context}{skills_section}"

        tools = self._tools
        skill_creator_path = BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md"
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": render_template(
                    "agent/dream_phase2.md",
                    strip=True,
                    skill_creator_path=str(skill_creator_path),
                ),
            },
            {"role": "user", "content": phase2_prompt},
        ]

        try:
            result = await self._runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                fail_on_tool_error=False,
            ))
            logger.debug(
                "Dream Phase 2 complete: stop_reason={}, tool_events={}",
                result.stop_reason, len(result.tool_events),
            )
            for ev in (result.tool_events or []):
                logger.info("Dream tool_event: name={}, status={}, detail={}", ev.get("name"), ev.get("status"), ev.get("detail", "")[:200])
        except Exception:
            logger.exception("Dream Phase 2 failed")
            result = None

        # Build changelog from tool events
        changelog: list[str] = []
        if result and result.tool_events:
            for event in result.tool_events:
                if event["status"] == "ok":
                    changelog.append(f"{event['name']}: {event['detail']}")

        # Only advance cursor on successful completion to prevent silent loss
        cursor_advanced = False
        if result and result.stop_reason == "completed":
            new_cursor = batch[-1]["cursor"]
            self.store.set_last_dream_cursor(new_cursor)
            cursor_advanced = True
            logger.info(
                "Dream done: {} change(s), cursor advanced to {}",
                len(changelog), new_cursor,
            )
        else:
            reason = result.stop_reason if result else "exception"
            logger.warning(
                "Dream incomplete ({}): cursor NOT advanced, will retry next cron cycle",
                reason,
            )

        self.store.compact_history()

        # Git auto-commit (only when there are actual changes)
        commit_sha: str | None = None
        if changelog and self.store.git.is_initialized():
            ts = batch[-1]["timestamp"]
            summary = f"dream: {ts}, {len(changelog)} change(s)"
            commit_msg = f"{summary}\n\n{analysis.strip()}"
            sha = self.store.git.auto_commit(commit_msg)
            if sha:
                commit_sha = sha
                logger.info("Dream commit: {}", sha)

        if cursor_advanced:
            end_status = "ok"
        elif result is None:
            end_status = "phase2_exception"
        else:
            end_status = f"phase2_{result.stop_reason}"
        emit_tool_event(
            "memory.dream.legacy.end",
            {
                "status": end_status,
                "duration_ms": int((_time.perf_counter() - t0) * 1000),
                "cursor_advanced": cursor_advanced,
                "changelog_count": len(changelog),
                "phase1_prompt_tokens": phase1_prompt_tokens,
                "phase1_completion_tokens": phase1_completion_tokens,
                "phase2_tool_events": (
                    len(result.tool_events) if result and result.tool_events else 0
                ),
                "commit_sha": commit_sha,
                "model": self.model,
            },
        )

        return True
