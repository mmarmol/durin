"""Session management for conversation history."""

import json
import os
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from durin.config.paths import get_legacy_sessions_dir
from durin.session.session_meta import (
    meta_path_for,
    read_derived,
    write_derived,
)
from durin.utils.file_lock import cross_process_lock
from durin.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    find_legal_message_start,
    image_placeholder_text,
    safe_filename,
)
from durin.utils.subagent_channel_display import scrub_subagent_announce_body

FILE_MAX_MESSAGES = 2000
_MESSAGE_TIME_PREFIX_RE = re.compile(r"^\[Message Time: [^\]]+\]\n?")
_LOCAL_IMAGE_BREADCRUMB_RE = re.compile(r"^\[image: (?:/|~)[^\]]+\]\s*$")
_TOOL_CALL_ECHO_RE = re.compile(r'^\s*message\([^)]*\)\s*$')
_SESSION_PREVIEW_MAX_CHARS = 120


def _sanitize_assistant_replay_text(content: str) -> str:
    """Remove internal replay artifacts that the model may have copied before.

    These strings are useful as runtime/session metadata, but when they appear
    in assistant examples they become demonstrations for the model to repeat.
    """
    content = _MESSAGE_TIME_PREFIX_RE.sub("", content, count=1)
    lines = [
        line
        for line in content.splitlines()
        if not _LOCAL_IMAGE_BREADCRUMB_RE.match(line)
        and not _TOOL_CALL_ECHO_RE.match(line)
    ]
    return "\n".join(lines).strip()


def _text_preview(content: Any) -> str:
    """Return compact display text for session lists."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        text = " ".join(parts)
    else:
        return ""
    text = _sanitize_assistant_replay_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _SESSION_PREVIEW_MAX_CHARS:
        text = text[: _SESSION_PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return text


def _message_preview_text(message: dict[str, Any]) -> str:
    """Session list preview text; subagent inject blobs are shortened for display."""
    content: Any = message.get("content")
    if message.get("injected_event") == "subagent_result" and isinstance(content, str):
        content = scrub_subagent_announce_body(content)
    return _text_preview(content)


def is_workflow_session(key: str) -> bool:
    """A workflow node/run session (keyed ``workflow:<run_id>:...``) is internal
    execution machinery — the per-node conversation, persisted only so the run-detail
    view can show it. It is NOT a user chat: excluded from the session list, the FTS
    index, and the memory graph/entity dream (the run-detail view reaches it by key, not
    via the list or a search). The .jsonl stays on disk; it just earns no .md/index entry."""
    return key.startswith("workflow:")


def is_workflow_session_file(path: "str | Path") -> bool:
    """``is_workflow_session`` for an on-disk session file: the key→file mapping turns
    ':' into '_', so a ``workflow:`` session is stored as a ``workflow_…`` stem. Used by
    the memory passes that glob ``sessions/*.jsonl`` directly."""
    return Path(path).stem.startswith("workflow_")


@dataclass
class Session:
    """A conversation session."""

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files
    # Tombstone: set by ``delete_session`` so a stale reference held by an
    # in-flight turn cannot resurrect the file via a late ``save()``.
    _deleted: bool = field(default=False, repr=False, compare=False)

    @staticmethod
    def _annotate_message_time(message: dict[str, Any], content: Any) -> Any:
        """Expose persisted turn timestamps to the model for relative-date reasoning.

        Annotating *every* assistant turn trains the model (via in-context
        demonstrations) to start its own replies with the same
        ``[Message Time: ...]`` prefix, which leaks metadata back to the user.
        We therefore only annotate user turns. User-side stamps are enough to
        pin adjacent assistant replies for relative-time reasoning, including
        proactive messages the user replies to later.
        """
        timestamp = message.get("timestamp")
        if not timestamp or not isinstance(content, str):
            return content
        role = message.get("role")
        if role != "user":
            return content
        return f"[Message Time: {timestamp}]\n{content}"

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(
        self,
        max_messages: int = 480,
        *,
        max_tokens: int = 0,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input.

        History is sliced by message count first (``max_messages``), then by
        token budget from the tail (``max_tokens``) when provided.
        """
        unconsolidated = self.messages[self.last_consolidated:]
        max_messages = max_messages if max_messages > 0 else 480
        sliced = unconsolidated[-max_messages:]

        # Avoid starting mid-turn when possible, except for proactive
        # assistant deliveries that the user may be replying to.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                start = i
                if i > 0 and sliced[i - 1].get("_channel_delivery"):
                    start = i - 1
                sliced = sliced[start:]
                break

        # Drop orphan tool results at the front.
        start = find_legal_message_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            if message.get("_command"):
                continue
            content = message.get("content", "")
            role = message.get("role")
            if role == "assistant" and isinstance(content, str):
                content = _sanitize_assistant_replay_text(content)
            # Synthesize an ``[image: path]`` breadcrumb from the persisted
            # ``media`` kwarg so LLM replay still sees *something* where the
            # image used to be. Without this, an image-only user turn
            # replays as an empty user message — the assistant's reply then
            # looks like it's responding to nothing.
            media = message.get("media")
            if role == "user" and isinstance(media, list) and media and isinstance(content, str):
                breadcrumbs = "\n".join(
                    image_placeholder_text(p) for p in media if isinstance(p, str) and p
                )
                content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            if include_timestamps:
                content = self._annotate_message_time(message, content)
            if role == "assistant" and isinstance(content, str) and not content.strip():
                if not any(key in message for key in ("tool_calls", "reasoning_content", "thinking_blocks")):
                    continue
            entry: dict[str, Any] = {"role": message["role"], "content": content}
            for key in ("tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)

        if max_tokens > 0 and out:
            kept: list[dict[str, Any]] = []
            used = 0
            for message in reversed(out):
                tokens = estimate_message_tokens(message)
                if kept and used + tokens > max_tokens:
                    break
                kept.append(message)
                used += tokens
            kept.reverse()

            # Keep history aligned to the first visible user turn.
            first_user = next((i for i, m in enumerate(kept) if m.get("role") == "user"), None)
            if first_user is not None:
                kept = kept[first_user:]
            else:
                # Tight token budgets can otherwise leave assistant-only tails.
                # If a user turn exists in the unsliced output, recover the
                # nearest one even if it slightly exceeds the token budget.
                recovered_user = next(
                    (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
                    None,
                )
                if recovered_user is not None:
                    kept = out[recovered_user:]

            # And keep a legal tool-call boundary at the front.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
            out = kept
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()
        self.metadata.pop("_last_summary", None)

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        """Keep a legal recent suffix constrained by a hard message cap."""
        if max_messages <= 0:
            self.clear()
            return
        if len(self.messages) <= max_messages:
            return

        retained = list(self.messages[-max_messages:])

        # Prefer starting at a user turn when one exists within the tail.
        first_user = next((i for i, m in enumerate(retained) if m.get("role") == "user"), None)
        if first_user is not None:
            retained = retained[first_user:]
        else:
            # If the tail is assistant/tool-only, anchor to the latest user in
            # the full session and take a capped forward window from there.
            latest_user = next(
                (i for i in range(len(self.messages) - 1, -1, -1)
                 if self.messages[i].get("role") == "user"),
                None,
            )
            if latest_user is not None:
                retained = list(self.messages[latest_user: latest_user + max_messages])

        # Mirror get_history(): avoid persisting orphan tool results at the front.
        start = find_legal_message_start(retained)
        if start:
            retained = retained[start:]

        # Hard-cap guarantee: never keep more than max_messages.
        if len(retained) > max_messages:
            retained = retained[-max_messages:]
            start = find_legal_message_start(retained)
            if start:
                retained = retained[start:]

        dropped = len(self.messages) - len(retained)
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self.updated_at = datetime.now()

    def enforce_file_cap(
        self,
        on_archive: Any = None,
        limit: int = FILE_MAX_MESSAGES,
    ) -> None:
        """Bound session message growth by archiving and trimming old prefixes."""
        if limit <= 0 or len(self.messages) <= limit:
            return

        before = list(self.messages)
        before_last_consolidated = self.last_consolidated
        before_count = len(before)
        self.retain_recent_legal_suffix(limit)
        dropped_count = before_count - len(self.messages)
        if dropped_count <= 0:
            return

        dropped = before[:dropped_count]
        already_consolidated = min(before_last_consolidated, dropped_count)
        archive_chunk = dropped[already_consolidated:]
        if archive_chunk and on_archive:
            on_archive(archive_chunk)
        logger.info(
            "Session file cap hit for {}: dropped {}, raw-archived {}, kept {}",
            self.key,
            dropped_count,
            len(archive_chunk),
            len(self.messages),
        )


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory. The
    line-0 ``metadata`` header carries IDENTITY state (mode, plan path,
    todos, channel ownership, …) — anything a learning pipeline should
    treat as authoritative session content. LLM-DERIVED projections of
    that content (compaction summary today, future embeddings or
    narrative summaries) live in the sibling ``<key>.meta.json`` under
    a ``derived`` block. The in-memory ``Session.metadata`` dict merges
    both at load time so consumer code continues to see one flat dict.
    """

    # Keys in ``Session.metadata`` that are LLM-DERIVED projections of
    # the session content. At save time these get split out and written
    # to the sibling ``.meta.json`` ``derived`` block instead of line 0.
    # At load time they're merged back into ``Session.metadata`` so
    # consumer code doesn't have to know about the split.
    #
    # Add new entries here when introducing future derived state (e.g.
    # ``"session_embedding"``, ``"narrative_summary"``). Keys NOT listed
    # here flow through line 0 unchanged.
    _DERIVED_METADATA_KEYS = frozenset({"_last_summary", "_last_tags", "skill_calls"})

    # Keys in ``Session.metadata`` that hold volatile per-turn state —
    # mid-turn recovery payloads that must survive a process restart but
    # must NOT bloat or rewrite the ``.jsonl`` transcript on every
    # checkpoint write. They are persisted exclusively to the
    # ``.meta.json`` ``derived`` block (alongside the LLM-derived keys)
    # and merged back into ``Session.metadata`` on load, so existing
    # readers (``session.metadata["runtime_checkpoint"]``) are unchanged.
    #
    # These correspond to ``AgentLoop._RUNTIME_CHECKPOINT_KEY`` and
    # ``AgentLoop._PENDING_USER_TURN_KEY`` in ``durin/agent/loop.py``.
    # Sidecar split: volatile keys are written to a separate .sidecar file
    # so per-turn saves avoid a full .jsonl rewrite.
    _VOLATILE_METADATA_KEYS = frozenset({"runtime_checkpoint", "pending_user_turn"})

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

    @staticmethod
    def safe_key(key: str) -> str:
        """Public helper used by HTTP handlers to map an arbitrary key to a stable filename stem."""
        return safe_filename(key.replace(":", "_"))

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        return self.sessions_dir / f"{self.safe_key(key)}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy session path under the instance home (``<DURIN_HOME>/sessions/``)."""
        return self.legacy_sessions_dir / f"{self.safe_key(key)}.jsonl"

    def exists(self, key: str) -> bool:
        """True when the session is cached or present on disk.

        Lets out-of-band mutators (rename) keep their 404-on-missing
        behaviour while still routing through :meth:`get_or_create` to
        share the cached instance.
        """
        return key in self._cache or self._get_session_path(key).exists()

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def reload(self, key: str) -> "Session":
        """Drop any cached copy and re-read the session from disk.

        The load-per-turn read path: a long-lived process must call this at
        the start of each turn so it sees turns appended by another process.
        """
        self._cache.pop(key, None)
        return self.get_or_create(key)

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            updated_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        updated_at = datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            # Merge derived metadata from the sidecar into the
            # in-memory ``metadata`` dict so consumer code keeps reading
            # one flat dict. Sidecar wins over line-0 for derived keys
            # (line-0 values are only present in legacy sessions written
            # before the split; the next save will clean them out).
            self._merge_derived_from_sidecar(key, metadata)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            repaired = self._repair(key)
            if repaired is not None:
                logger.info("Recovered session {} from corrupt file ({} messages)", key, len(repaired.messages))
            return repaired

    def _merge_derived_from_sidecar(
        self, key: str, metadata: dict[str, Any],
    ) -> None:
        """Layer derived state from ``<key>.meta.json`` over the
        line-0 metadata dict.

        Sidecar values WIN where both files declare the same derived
        key — this lets the split persist correctly even when a legacy
        session.jsonl still carries the old in-line copy. Best-effort:
        a missing or malformed sidecar leaves ``metadata`` untouched."""
        try:
            sidecar = read_derived(meta_path_for(key, self.sessions_dir))
        except Exception:
            logger.exception(
                "Failed to read derived sidecar for {}; using line-0 only", key,
            )
            return
        _sidecar_keys = self._DERIVED_METADATA_KEYS | self._VOLATILE_METADATA_KEYS
        for sidecar_key, value in sidecar.items():
            if sidecar_key in _sidecar_keys:
                metadata[sidecar_key] = value

    def _repair(self, key: str) -> Session | None:
        """Attempt to recover a session from a corrupt JSONL file."""
        path = self._get_session_path(key)
        if not path.exists():
            return None

        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: datetime | None = None
            updated_at: datetime | None = None
            last_consolidated = 0
            skipped = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        skipped += 1
                        continue

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        if data.get("created_at"):
                            with suppress(ValueError, TypeError):
                                created_at = datetime.fromisoformat(data["created_at"])
                        if data.get("updated_at"):
                            with suppress(ValueError, TypeError):
                                updated_at = datetime.fromisoformat(data["updated_at"])
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            if skipped:
                logger.warning("Skipped {} corrupt lines in session {}", skipped, key)

            if not messages and not metadata:
                return None

            self._merge_derived_from_sidecar(key, metadata)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Repair failed for session {}: {}", key, e)
            return None

    @staticmethod
    def _session_payload(session: Session) -> dict[str, Any]:
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def save(self, session: Session, *, fsync: bool = False, reindex: bool = True) -> None:
        """Save a session to disk atomically.

        Splits ``session.metadata`` into two persistence layers:

        - Identity fields → ``<key>.jsonl`` line 0 (source of truth).
        - LLM-derived projections (``_DERIVED_METADATA_KEYS``) → the
          ``derived`` block of the sibling ``<key>.meta.json``.

        Keeps ``session.metadata`` in memory unchanged so existing
        consumer code reads it through the same dict.

        When *fsync* is ``True`` the final file and its parent directory are
        explicitly flushed to durable storage.  This is intentionally off by
        default (the OS page-cache is sufficient for normal operation) but
        should be enabled during graceful shutdown so that filesystems with
        write-back caching (e.g. rclone VFS, NFS, FUSE mounts) do not lose
        the most recent writes.

        When *reindex* is ``False``, the regeneration of the ``.md`` view and
        FTS indexing are skipped. Use this to defer regeneration to a separate
        ``reindex_session()`` call (e.g., off the event-loop thread).
        """
        if getattr(session, "_deleted", False):
            logger.debug("Skipping save of deleted session {}", session.key)
            return

        path = self._get_session_path(session.key)
        tmp_path = path.with_suffix(".jsonl.tmp")

        # Split metadata for persistence; in-memory dict stays whole.
        # Both derived (LLM-projected) and volatile (per-turn recovery)
        # keys are excluded from line-0 and written to the sidecar only.
        _sidecar_keys = self._DERIVED_METADATA_KEYS | self._VOLATILE_METADATA_KEYS
        identity_meta = {
            k: v for k, v in session.metadata.items()
            if k not in _sidecar_keys
        }
        derived_meta = {
            k: v for k, v in session.metadata.items()
            if k in _sidecar_keys
        }

        with cross_process_lock(path):
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    metadata_line = {
                        "_type": "metadata",
                        "key": session.key,
                        "created_at": session.created_at.isoformat(),
                        "updated_at": session.updated_at.isoformat(),
                        "metadata": identity_meta,
                        "last_consolidated": session.last_consolidated
                    }
                    f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                    for msg in session.messages:
                        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                    if fsync:
                        f.flush()
                        os.fsync(f.fileno())

                os.replace(tmp_path, path)

                if fsync:
                    # fsync the directory so the rename is durable.
                    # On Windows, opening a directory with O_RDONLY raises
                    # PermissionError — skip the dir sync there (NTFS
                    # journals metadata synchronously).
                    with suppress(PermissionError):
                        fd = os.open(str(path.parent), os.O_RDONLY)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise

            # Mirror derived metadata into the .meta.json sidecar. Always
            # invoked (even with an empty dict) so that clearing a summary
            # via ``Session.clear()`` also wipes the persisted version.
            # Best-effort: a meta.json write failure must not bring down the
            # session save — the .jsonl is the durable surface.
            try:
                write_derived(
                    meta_path_for(session.key, self.sessions_dir),
                    session.key,
                    derived_meta,
                )
            except Exception:
                logger.exception(
                    "Failed to write derived meta for {}", session.key,
                )

            # Regenerate the navigable `<key>.md` view alongside the jsonl so
            # memory entries can reference specific turns by markdown anchor, then
            # FTS-index it. Skipped for workflow node/run sessions: they are internal
            # execution traces, not user chats, so they earn no .md, no lexical index,
            # and (since the entity dream reads the .md) no place in the memory graph.
            # Best-effort: a regeneration/index failure must not bring down the session
            # save — the jsonl is the durable surface.
            if reindex and not is_workflow_session(session.key):
                try:
                    from durin.memory.session_md import regenerate_session_md

                    regenerate_session_md(path)
                except Exception:
                    logger.warning(
                        "Failed to regenerate session markdown view for {}",
                        session.key,
                    )
                else:
                    try:
                        from durin.memory.indexer import reindex_session_file

                        reindex_session_file(
                            self.workspace, path.with_suffix(".md"),
                        )
                    except Exception:
                        logger.warning(
                            "Failed to index session turns for {}", session.key,
                        )

        self._cache[session.key] = session

    def reindex_session(self, key: str) -> None:
        """Regenerate the ``<key>.md`` view and incrementally FTS-index its new
        turns. Split out of ``save`` so the agent loop can run it off the
        event-loop thread via ``asyncio.to_thread``. Skips workflow sessions
        (they earn no .md/index) and missing files. Best-effort — a failure is
        logged and never raised. Takes the session's ``.jsonl`` lock so it
        renders from a consistent transcript."""
        if is_workflow_session(key):
            return
        path = self._get_session_path(key)
        if not path.exists():
            return
        try:
            with cross_process_lock(path):
                try:
                    from durin.memory.session_md import regenerate_session_md

                    regenerate_session_md(path)
                except Exception:
                    logger.warning(
                        "Failed to regenerate session markdown view for {}", key,
                    )
                    return
                try:
                    from durin.memory.indexer import reindex_session_file

                    reindex_session_file(self.workspace, path.with_suffix(".md"))
                except Exception:
                    logger.warning("Failed to index session turns for {}", key)
        except Exception:
            # Lock acquisition (e.g. TimeoutError) must never raise into the
            # background reindex drainer — reindex is best-effort.
            logger.warning("reindex_session could not lock/complete for {}", key)

    def flush_all(self) -> int:
        """Re-save every cached session with fsync for durable shutdown.

        Returns the number of sessions flushed.  Errors on individual
        sessions are logged but do not prevent other sessions from being
        flushed.
        """
        flushed = 0
        for key, session in list(self._cache.items()):
            try:
                self.save(session, fsync=True)
                flushed += 1
            except Exception:
                logger.warning("Failed to flush session {}", key, exc_info=True)
        return flushed

    def save_runtime_state(self, session: Session) -> None:
        """Write the sidecar volatile (+derived) block without touching the ``.jsonl``.

        Use this instead of ``save()`` for mid-turn checkpoint writes
        (``runtime_checkpoint``, ``pending_user_turn``) — it avoids the
        full-file rewrite of the ``.jsonl``, the ``.md`` regeneration, and
        the FTS reindex that ``save()`` performs.  The volatile keys in
        ``session.metadata`` are still merged back on load so existing readers
        are unchanged.

        Sidecar split: volatile keys go to a separate .sidecar file,
        avoiding a full .jsonl rewrite on every turn.
        """
        if getattr(session, "_deleted", False):
            logger.debug("Skipping save_runtime_state of deleted session {}", session.key)
            return

        path = self._get_session_path(session.key)
        _sidecar_keys = self._DERIVED_METADATA_KEYS | self._VOLATILE_METADATA_KEYS
        sidecar_meta = {
            k: v for k, v in session.metadata.items()
            if k in _sidecar_keys
        }

        with cross_process_lock(path):
            try:
                write_derived(
                    meta_path_for(session.key, self.sessions_dir),
                    session.key,
                    sidecar_meta,
                )
            except Exception:
                logger.exception(
                    "Failed to write runtime state sidecar for {}", session.key,
                )

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def delete_session(self, key: str) -> bool:
        """Remove a session from disk and the in-memory cache.

        Removes both the ``<key>.jsonl`` and the sibling ``<key>.meta.json``
        sidecar so derived state doesn't outlive its source. Returns True
        when at least the JSONL was found and unlinked.
        """
        path = self._get_session_path(key)
        meta_path = meta_path_for(key, self.sessions_dir)
        # Tombstone any cached instance first: a turn still holding this
        # object would otherwise resurrect the file on its end-of-turn save.
        cached = self._cache.get(key)
        if cached is not None:
            cached._deleted = True
        self.invalidate(key)
        # Sidecar may exist even when the jsonl is already gone (orphan
        # from a prior crash) — best-effort cleanup either way.
        if meta_path.exists():
            with suppress(OSError):
                meta_path.unlink()
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.warning("Failed to delete session file {}: {}", path, e)
            return False

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        """Load a session from disk without caching; intended for read-only HTTP endpoints.

        Returns ``{"key", "created_at", "updated_at", "metadata", "messages"}`` or
        ``None`` when the session file does not exist or fails to parse.
        """
        path = self._get_session_path(key)
        if not path.exists():
            return None
        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: str | None = None
            updated_at: str | None = None
            stored_key: str | None = None
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = data.get("created_at")
                        updated_at = data.get("updated_at")
                        stored_key = data.get("key")
                    else:
                        messages.append(data)
            # Return the same merged view the live ``Session`` uses, so
            # HTTP read endpoints stay consistent across read paths.
            merged_metadata = dict(metadata) if isinstance(metadata, dict) else {}
            self._merge_derived_from_sidecar(stored_key or key, merged_metadata)
            return {
                "key": stored_key or key,
                "created_at": created_at,
                "updated_at": updated_at,
                "metadata": merged_metadata,
                "messages": messages,
            }
        except Exception as e:
            logger.warning("Failed to read session {}: {}", key, e)
            repaired = self._repair(key)
            if repaired is not None:
                logger.info("Recovered read-only session view {} from corrupt file", key)
                return self._session_payload(repaired)
            return None

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            fallback_key = path.stem.replace("_", ":", 1)
            # Workflow node/run sessions are internal execution traces, not user chats —
            # keep them out of the listing (the run-detail view reaches them by key). The
            # key→file mapping turns ':' into '_', so the restored fallback_key carries the
            # 'workflow:' prefix even before the file is read.
            if is_workflow_session(fallback_key):
                continue
            try:
                # Read the metadata line and a small preview for WebUI/session lists.
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            metadata = data.get("metadata", {})
                            title = metadata.get("title") if isinstance(metadata, dict) else None
                            preview = ""
                            fallback_preview = ""
                            for line in f:
                                if not line.strip():
                                    continue
                                item = json.loads(line)
                                if item.get("_type") == "metadata":
                                    continue
                                text = _message_preview_text(item)
                                if not text:
                                    continue
                                if item.get("role") == "user":
                                    preview = text
                                    break
                                if not fallback_preview and item.get("role") == "assistant":
                                    fallback_preview = text
                            preview = preview or fallback_preview
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "title": title if isinstance(title, str) else "",
                                "preview": preview,
                                "path": str(path)
                            })
            except Exception:
                repaired = self._repair(fallback_key)
                if repaired is not None:
                    sessions.append({
                        "key": repaired.key,
                        "created_at": repaired.created_at.isoformat(),
                        "updated_at": repaired.updated_at.isoformat(),
                        "title": (
                            repaired.metadata.get("title")
                            if isinstance(repaired.metadata.get("title"), str)
                            else ""
                        ),
                        "preview": next(
                            (
                                text
                                for msg in repaired.messages
                                if (text := _message_preview_text(msg))
                            ),
                            "",
                        ),
                        "path": str(path)
                    })
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def children_of(self, parent_session_id: str) -> list[dict[str, Any]]:
        """Session info for every persisted session whose lineage parent is
        *parent_session_id*.

        Globs the sessions dir and reads only the line-0 metadata header
        (cheap), so it works from a cold cache. Used to navigate from a
        session to the branch sessions it spawned — subagents today,
        workflow stages later. Lineage keys live on line 0 (identity
        metadata), so no full-file load is needed.
        """
        from durin.session.lineage import (
            ORIGIN_ID,
            ORIGIN_TYPE,
            PARENT_SESSION_ID,
        )

        out: list[dict[str, Any]] = []
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue
                data = json.loads(first_line)
                if data.get("_type") != "metadata":
                    continue
                meta = data.get("metadata") or {}
                if meta.get(PARENT_SESSION_ID) != parent_session_id:
                    continue
                out.append({
                    "key": data.get("key") or path.stem.replace("_", ":", 1),
                    "origin_type": meta.get(ORIGIN_TYPE),
                    "origin_id": meta.get(ORIGIN_ID),
                    "created_at": data.get("created_at"),
                    "path": str(path),
                    "title": meta.get("title"),
                })
            except Exception:
                continue
        return sorted(out, key=lambda x: x.get("created_at") or "")
