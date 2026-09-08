"""Session summaries as markdown projections under `memory/session_summary/`.

Previously, session summaries lived as
`session.metadata["_last_summary"] = {"text", "last_active"}` inside
`<session_key>.meta.json`. The hot layer read them directly from the
in-memory dict; nothing indexed them, and the walker only iterates
`.md` under `memory/`.

This module picks the single-source-of-truth path: the
summary lives ONLY in `memory/session_summary/<sanitized_key>.md`.
The JSON metadata stops carrying `_last_summary` going forward.
This module owns the write / read / sanitize and the one-shot
migration of pre-A10 sessions that still have the legacy field.

Why this module instead of inlining the file I/O in `agent/memory.py`:

- The same read path is needed by the hot layer (`agent/memory.py`
  + `agent/loop.py`) and indirectly by the search pipeline (via the
  walker → indexer → `class_name="session_summary"`).
- Sanitisation of session keys (which can contain ``:`` or other
  unsafe chars on channels like ``telegram:<id>``) lives in one
  place.
- The migration helper keeps backward compatibility silent — old
  sessions with `_last_summary` in the JSON keep working until they
  next compact.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Sequence, Tuple

from durin.memory.paths import memory_class_dir
from durin.memory.schema import MemoryEntry
from durin.memory.storage import load_entry, save_entry
from durin.utils.file_lock import cross_process_lock

logger = logging.getLogger(__name__)

__all__ = [
    "SESSION_SUMMARY_CLASS",
    "append_session_summary_block",
    "closed_record_key",
    "delete_session_summary",
    "find_previous_session_summary",
    "get_session_summary",
    "get_session_summary_tags",
    "sanitize_session_key",
    "session_summary_path",
    "write_session_summary",
]


SESSION_SUMMARY_CLASS = "session_summary"

# Same shape as `TelemetryLogger`'s key sanitiser: collapse anything
# that isn't a word char or dash into `_`, drop runs of dots so a
# path-like key can't escape the directory. Cap at 80 chars to keep
# the filename in any filesystem's name limit.
_SAFE_KEY_RE = re.compile(r"[^\w\-]")
_DOT_RUN_RE = re.compile(r"\.{2,}")


def sanitize_session_key(key: str) -> str:
    """Map an arbitrary session key to a filename-safe identifier.

    Mirrors the convention used by ``durin.telemetry.logger
    .get_session_logger`` so a single session key sanitises the same
    way wherever the system touches it.
    """
    safe = _SAFE_KEY_RE.sub("_", key)[:80]
    safe = _DOT_RUN_RE.sub("_", safe)
    return safe or "default"


def closed_record_key(key: str, when: datetime) -> str:
    """Record key for the conversation ``key`` closes at ``when``.

    Truncates the sanitized session key to 40 chars *before* appending the
    ``_closed_<timestamp>`` suffix, so a long ``key`` cannot push the
    suffix past ``sanitize_session_key``'s 80-char cap (which would
    silently drop it, making two closes of the same long key collide into
    one file). The result contains only word chars, dashes and
    underscores, so sanitizing it again — as ``write_session_summary``
    does — leaves it unchanged.
    """
    return f"{sanitize_session_key(key)[:40]}_closed_{when.strftime('%Y%m%dT%H%M%S')}"


def session_summary_path(workspace: Path, session_key: str) -> Path:
    """Resolve the markdown path for a session's summary."""
    return memory_class_dir(workspace, SESSION_SUMMARY_CLASS) / (
        f"{sanitize_session_key(session_key)}.md"
    )


def _headline_from(text: str) -> str:
    """First ~10 words of *text*, used as the entry headline."""
    words = text.strip().split()
    return " ".join(words[:10]) if words else "session summary"


def _parse_last_active(value: object) -> Optional[date]:
    """Best-effort date parse for the ``last_active`` ISO string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
    return None


def write_session_summary(
    workspace: Path,
    session_key: str,
    text: str,
    last_active: object = None,
    *,
    headline_source: Optional[str] = None,
    entities: Optional[Sequence[str]] = None,
    topics: Optional[Sequence[str]] = None,
) -> Optional[Path]:
    """Persist *text* as `memory/session_summary/<sanitized>.md`.

    Returns the path written, or ``None`` when *text* is empty
    (degenerate input is treated as "no summary" rather than
    written as a zero-byte entry).

    The entry id is the sanitised session key, so re-writing the
    summary for the same session overwrites the previous file —
    update semantics, not append.

    ``headline_source``, when given, is used to derive the headline
    instead of *text*. Callers that append bounded blocks (see
    ``append_session_summary_block``) pass the newest span block here
    so the headline still summarizes recent content once older blocks
    (or a synthetic carried-paths head block) are evicted to the front
    of *text*.

    ``entities`` / ``topics`` are the tags the archive prompt returns
    alongside the bullets. They land in the entry's frontmatter, which
    is what the FTS and vector composers read, so a summary stays
    reachable by a name or subject its prose never spells out. Entity
    refs must already be well-formed ``<type>:<value>`` — the schema
    validates them and a bad ref raises rather than writing a broken
    entry.
    """
    text = (text or "").strip()
    if not text or text == "(nothing)":
        return None

    sanitized = sanitize_session_key(session_key)
    valid_from = _parse_last_active(last_active) or date.today()
    entry = MemoryEntry(
        id=sanitized,
        headline=_headline_from(headline_source if headline_source is not None else text),
        summary=text,
        body=text,
        entities=list(entities or ()),
        topics=list(topics or ()),
        author="agent_created",
        valid_from=valid_from,
    )

    target = session_summary_path(workspace, session_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    save_entry(entry, target)
    return target


_SUMMARY_BLOCK_SEP = "\n\n---\n"
_SESSION_SUMMARY_MAX_CHARS = 16_000
# Path trailers from evicted blocks are carried forward in a synthetic
# head block so the mechanical path guarantee outlives block eviction.
_SPAN_PATHS_PREFIX = "Files/paths examined in this span"
_EVICTED_PATHS_PREFIX = "Files/paths from earlier spans (evicted): "
_EVICTED_PATHS_MAX_CHARS = 1_200
# The tag union (see `append_session_summary_block`) accumulates for the
# life of the key — every compaction span and nightly pass adds to it, and
# nothing ever removes a tag on its own. Left uncapped it eventually
# starves `VectorIndex._embed_text`'s tag-line budget (spent before the
# body) and floods `_render_block`'s `Entities:` tail. Capped to the most
# recently seen tags so the file stays a compact, high-signal set no
# matter how long the key has been compacting. Topics get a tighter cap
# than entities — they're meant to stay a short set of subject labels.
_MAX_SUMMARY_ENTITIES = 24
_MAX_SUMMARY_TOPICS = 12


def _salvage_paths(evicted_block: str, carried: list[str]) -> None:
    """Collect path-trailer entries from an evicted block into *carried*."""
    for line in evicted_block.splitlines():
        if line.startswith(_SPAN_PATHS_PREFIX) or line.startswith(_EVICTED_PATHS_PREFIX):
            _, _, tail = line.partition(": ")
            for path in tail.split("; "):
                path = path.strip()
                if path and path not in carried:
                    carried.append(path)


def _build_carried_line(carried: list[str]) -> str:
    """Join carried path entries into a line bounded by
    ``_EVICTED_PATHS_MAX_CHARS``.

    Drops whole entries rather than raw-slicing the joined string, so
    the result never ends in a truncated path fragment that
    ``_salvage_paths`` would later mis-parse as a real path.
    *carried* accumulates oldest-first; entries are dropped from the
    oldest end so the newest carried paths survive the cap.
    """
    kept: list[str] = []
    total = len(_EVICTED_PATHS_PREFIX)
    for path in reversed(carried):
        added = len(path) + (2 if kept else 0)  # "; " separator
        if total + added > _EVICTED_PATHS_MAX_CHARS:
            break
        kept.insert(0, path)
        total += added
    return _EVICTED_PATHS_PREFIX + "; ".join(kept)


def _merge_tags(prior: list[str], new: Optional[Sequence[str]], cap: int) -> list[str]:
    """Union *prior* and *new*, keeping the *cap* most recently seen tags.

    *prior*'s own order is preserved and any tag in *new* not already
    present is appended after it — recency order, not alphabetical, so
    "most recently seen" is well-defined. Once the merged list exceeds
    *cap*, entries are dropped from the front (the oldest survivors),
    never from *new* preferentially over *prior* or vice versa — only
    position (how long ago a tag was last (re)confirmed) decides.
    """
    merged = list(prior)
    for tag in new or ():
        if tag not in merged:
            merged.append(tag)
    if len(merged) > cap:
        merged = merged[-cap:]
    return merged


def append_session_summary_block(
    workspace: Path,
    session_key: str,
    block: str,
    *,
    last_active: object = None,
    max_chars: int = _SESSION_SUMMARY_MAX_CHARS,
    entities: Optional[Sequence[str]] = None,
    topics: Optional[Sequence[str]] = None,
) -> Optional[Path]:
    """Append *block* to the session summary, evicting oldest blocks over cap.

    Each consolidation span contributes one block; the newest block always
    survives, so a single oversized block degrades to update semantics
    rather than an empty summary. Path trailers of evicted blocks are
    carried forward in a bounded synthetic head block — general facts
    wash out at the cap horizon (long-horizon recall is the memory
    system's job), discovered paths do not.

    ``entities`` / ``topics`` are this span's tags. The entry holds one
    list of each for the whole file, so they accumulate as a recency-order
    union over every span, capped to ``_MAX_SUMMARY_ENTITIES`` /
    ``_MAX_SUMMARY_TOPICS`` — the summary stays reachable by anything any
    of its *recent* spans was about, including spans whose text the block
    cap already evicted, without growing the tag list without bound. New
    tags alone are reason enough to rewrite: a repeated block contributes
    nothing to the text but its tags must still land.

    The read-rebuild-rewrite runs under the summary file's own
    ``cross_process_lock``: the compactor (gateway process) and the nightly
    session-summary pass (dream worker subprocess) both append here, and
    interleaved rounds would drop one side's block.
    """
    block = (block or "").strip()
    if not block or block == "(nothing)":
        return None
    with cross_process_lock(session_summary_path(workspace, session_key)):
        entry = _load_summary_entry(session_summary_path(workspace, session_key))
        existing = (entry.body or entry.summary or None) if entry else None
        prior_entities = list(entry.entities) if entry else []
        prior_topics = list(entry.topics) if entry else []
        merged_entities = _merge_tags(prior_entities, entities, _MAX_SUMMARY_ENTITIES)
        merged_topics = _merge_tags(prior_topics, topics, _MAX_SUMMARY_TOPICS)
        tags_changed = (
            merged_entities != prior_entities or merged_topics != prior_topics
        )
        blocks = [
            b.strip() for b in (existing.split(_SUMMARY_BLOCK_SEP) if existing else [])
            if b.strip()
        ]
        carried: list[str] = []
        if blocks and blocks[0].startswith(_EVICTED_PATHS_PREFIX):
            _salvage_paths(blocks.pop(0), carried)
        if blocks and blocks[-1] == block:
            blocks_changed = False  # degraded-LLM duplicate round: skip re-append
        else:
            blocks.append(block)
            blocks_changed = True
        while len(blocks) > 1 and (
            sum(len(b) for b in blocks)
            + len(_SUMMARY_BLOCK_SEP) * (len(blocks) - 1)
        ) > max_chars:
            _salvage_paths(blocks.pop(0), carried)
        if carried:
            blocks.insert(0, _build_carried_line(carried))
        if not blocks_changed and not carried and not tags_changed:
            return None
        return write_session_summary(
            workspace, session_key, _SUMMARY_BLOCK_SEP.join(blocks),
            last_active=last_active, headline_source=block,
            entities=merged_entities, topics=merged_topics,
        )


def _load_summary_entry(path: Path) -> Optional[MemoryEntry]:
    """Parse the summary entry at *path*, or ``None`` when it is absent or
    unreadable. Best-effort — a broken file must never break the compaction
    or the dream pass that is trying to append to it."""
    if not path.is_file():
        return None
    try:
        return load_entry(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "session_summary: failed to load %s: %s", path, exc,
        )
        return None


def get_session_summary(
    workspace: Path,
    session_key: str,
) -> Tuple[Optional[str], Optional[date]]:
    """Read the summary `.md` for *session_key*.

    Returns ``(text, last_active)`` or ``(None, None)`` when the
    file doesn't exist or doesn't parse. Best-effort — never raises.
    """
    entry = _load_summary_entry(session_summary_path(workspace, session_key))
    if entry is None:
        return (None, None)
    return (entry.body or entry.summary or None, entry.valid_from)


def get_session_summary_tags(
    workspace: Path,
    session_key: str,
) -> dict[str, list[str]]:
    """``{"entities": [...], "topics": [...]}`` of *session_key*'s summary,
    both empty when there is none — the same tags-dict shape every other
    producer (``parse_consolidator_response``, ``Consolidator._collect_tags``)
    uses, so callers don't juggle two different tag shapes.

    Callers that fold one summary's text into a different record — ``/new``
    building its closed-conversation record — carry its tags across too, or
    the record ends up searchable by less than the text it holds.
    """
    entry = _load_summary_entry(session_summary_path(workspace, session_key))
    if entry is None:
        return {"entities": [], "topics": []}
    return {"entities": list(entry.entities), "topics": list(entry.topics)}


def find_previous_session_summary(
    workspace: Path, session_key: str, *, channels: "set[str] | frozenset[str]",
) -> Optional[Tuple[str, str, Optional[date]]]:
    """``(previous_stem, text, last_active)`` for the newest OTHER session
    summary on ``session_key``'s channel, or ``None``.

    Only channels in ``channels`` qualify — those are the single-user
    surfaces where "the previous session" is the same person's. Files are
    the sanitized-key ``.md`` entries of this class; recency is the entry's
    ``valid_from`` (the session's last-active date), file mtime as tiebreak.
    """
    channel = session_key.split(":", 1)[0]
    if channel not in channels:
        return None
    own_stem = sanitize_session_key(session_key)
    prefix = sanitize_session_key(channel + ":")
    directory = memory_class_dir(workspace, SESSION_SUMMARY_CLASS)
    if not directory.is_dir():
        return None
    best: Optional[Tuple[Tuple[date, float], str, str, Optional[date]]] = None
    for md in directory.glob(f"{prefix}*.md"):
        if md.stem == own_stem:
            continue
        try:
            entry = load_entry(md)
        except Exception:  # noqa: BLE001 — a broken file is not "the previous session"
            continue
        text = entry.body or entry.summary
        if not text:
            continue
        rank = (entry.valid_from or date.min, md.stat().st_mtime)
        if best is None or rank > best[0]:
            best = (rank, md.stem, text, entry.valid_from)
    return None if best is None else (best[1], best[2], best[3])


def delete_session_summary(
    workspace: Path,
    session_key: str,
) -> bool:
    """Drop the `.md` for *session_key* if it exists.

    Used by the `/new` command: the file is the key's archived-context
    slot, replayed on every turn, so a fresh conversation on the key
    starts without it. (`Session.clear()` does not call this — it only
    resets the in-memory state.) Returns True iff a file was actually
    deleted.
    """
    path = session_summary_path(workspace, session_key)
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        logger.warning(
            "session_summary: failed to delete %s: %s", path, exc,
        )
        return False
    return True
