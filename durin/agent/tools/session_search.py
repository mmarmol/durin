"""``session_search`` tool — keyword/regex search across this session's messages.

Lets the model look up something it (or the user) said earlier without
re-reading every prior turn. The use case is long sessions where the
verbose history has scrolled out of the model's working context but
the model still needs to recall a specific decision, file path, error
message, or quoted snippet.

Returns a list of matches with ``msg_index`` (position in
``session.messages``), role, optional tool name, and a snippet of the
content around the match. The msg_index lets a follow-up read pull the
full message via the meta timeline or session inspection. The snippet
is the immediate context — usually enough to answer the question
without further lookups.

Design notes

- Search runs against the **live** ``session.messages`` list (in-memory,
  the same source of truth that the LLM history is built from). We do
  not read the live on-disk jsonl, which would be redundant and could
  drift mid-turn while messages are being appended.
- When the live scan yields fewer matches than requested and the session
  has **archive segments** (prefixes trimmed off the live file by the
  file cap — see ``SessionManager.append_to_archive``), the scan
  continues into them, newest segment first, in a worker thread with a
  hard byte budget. Archived hits carry a timestamp instead of a live
  ``msg_index`` (indexes are rebased on every trim, so a positional
  reference into the archive would lie).
- Both keyword (substring) and regex modes are supported. Keyword mode
  is the default — it is forgiving and fast; regex is opt-in for power
  users / chain-of-tool flows.
- The tool only exposes content the model has already produced or seen.
  No new data-leak surface vs. the existing history-rendering path.
- Allowed in every agent mode (read-only).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

if TYPE_CHECKING:
    from durin.session.manager import SessionManager


_MAX_RESULTS_CAP = 100
_MAX_SNIPPET_CHARS = 500
_MIN_SNIPPET_CHARS = 50
_DEFAULT_MAX_RESULTS = 20
_DEFAULT_SNIPPET_CHARS = 200
_TOTAL_OUTPUT_BUDGET = 10_000  # hard ceiling on the response string
_ALLOWED_ROLES = frozenset({"user", "assistant", "tool", "system"})
# Hard ceiling on bytes read from archive segments per call. The corpus is
# not the danger (segments are bounded and a linear scan of tens of MB is
# fast) — this guards the pathological many-segment archive and keeps one
# search call from turning into an unbounded disk walk. Segments are capped
# at 10MB, so at least three are always scannable.
_ARCHIVE_SCAN_MAX_BYTES = 32_000_000


def _extract_text(content: Any) -> str:
    """Flatten ``content`` (str | list of blocks | None) into searchable text.

    Mirrors the rendering path used elsewhere — string content is taken
    as-is, list-of-blocks contributes its ``text`` fields, anything else
    is treated as empty so we never crash on an exotic shape.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _match_message(
    msg: dict[str, Any],
    pattern: re.Pattern[str],
    role_filter: str | None,
    width: int,
) -> dict[str, Any] | None:
    """Match one message against the pattern; return the core hit entry.

    Shared by the live scan and the archive scan so the two can never
    drift in what counts as a match. Returns ``{role, snippet[, tool]}``
    or ``None``.
    """
    msg_role = msg.get("role")
    if role_filter is not None and msg_role != role_filter:
        return None
    text = _extract_text(msg.get("content"))
    if not text:
        return None
    m = pattern.search(text)
    if m is None:
        return None
    entry: dict[str, Any] = {
        "role": msg_role or "?",
        "snippet": _make_snippet(text, m.start(), m.end(), width),
    }
    if msg_role == "tool":
        tool_name = msg.get("name")
        if isinstance(tool_name, str) and tool_name:
            entry["tool"] = tool_name
    return entry


def _scan_archive(
    paths: list[Path],
    pattern: re.Pattern[str],
    role_filter: str | None,
    needed: int,
    width: int,
    byte_budget: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Scan archive segments for matches, newest segment first.

    Runs in a worker thread (blocking file IO + JSON parsing must not sit
    on the event loop). Stops descending into older segments once ``needed``
    matches exist or the byte budget is spent. Returns
    ``(matches_in_chronological_order, segments_scanned, complete)`` —
    ``complete`` is False when older segments were left unscanned, which
    only ever hides matches the result cap would have dropped anyway.
    """
    collected: list[list[dict[str, Any]]] = []
    found = 0
    bytes_read = 0
    scanned = 0
    complete = True
    for path in reversed(paths):  # newest segment first
        if found >= needed:
            complete = False
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if bytes_read and bytes_read + size > byte_budget:
            complete = False
            break
        seg_matches: list[dict[str, Any]] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    entry = _match_message(msg, pattern, role_filter, width)
                    if entry is None:
                        continue
                    ts = msg.get("timestamp")
                    if isinstance(ts, str) and ts:
                        entry["ts"] = ts[:16]
                    entry["archived"] = True
                    seg_matches.append(entry)
        except OSError:
            continue
        bytes_read += size
        scanned += 1
        if seg_matches:
            found += len(seg_matches)
            collected.append(seg_matches)
    # Segments were visited newest→oldest; flip so the flattened list is
    # chronological (oldest first), matching the live list's ordering.
    collected.reverse()
    return [m for seg in collected for m in seg], scanned, complete


def _make_snippet(text: str, start: int, end: int, width: int) -> str:
    """Return a ``width``-char window around the match, with ellipses."""
    if width <= 0 or not text:
        return ""
    half = max(1, (width - (end - start)) // 2)
    lo = max(0, start - half)
    hi = min(len(text), end + half)
    snippet = text[lo:hi]
    if lo > 0:
        snippet = "…" + snippet
    if hi < len(text):
        snippet = snippet + "…"
    # Collapse runs of whitespace for a readable single-line snippet.
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if len(snippet) > width + 4:
        snippet = snippet[: width + 3] + "…"
    return snippet


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema(
            description=(
                "Keyword (substring) or regex to find. Use a specific "
                "phrase — full sentences match too narrowly, single words "
                "match too broadly. 3-10 chars is the sweet spot."
            ),
            min_length=1,
            max_length=500,
        ),
        regex=BooleanSchema(
            description=(
                "Treat `query` as a Python regular expression. Default "
                "false (substring match). Use sparingly — invalid "
                "patterns surface as tool errors."
            ),
            nullable=True,
        ),
        case_sensitive=BooleanSchema(
            description=(
                "Case-sensitive match. Default false — most natural "
                "lookups are case-insensitive."
            ),
            nullable=True,
        ),
        role=StringSchema(
            description=(
                "Restrict search to messages of a specific role: "
                "'user', 'assistant', 'tool', or 'system'. Omit to "
                "search all roles."
            ),
            enum=("user", "assistant", "tool", "system"),
            nullable=True,
        ),
        max_results=IntegerSchema(
            description=(
                "Maximum number of matches to return. Default 20, max 100. "
                "Results are returned in chronological order; the cap "
                "applies to the **last** N matches when there are more."
            ),
            minimum=1,
            maximum=_MAX_RESULTS_CAP,
            nullable=True,
        ),
        snippet_chars=IntegerSchema(
            description=(
                "Approximate width of the snippet around each match, in "
                "characters. Default 200, range 50-500."
            ),
            minimum=_MIN_SNIPPET_CHARS,
            maximum=_MAX_SNIPPET_CHARS,
            nullable=True,
        ),
        required=["query"],
    )
)
class SessionSearchTool(Tool, ContextAware):
    """Search the current session's messages for keyword or regex matches."""

    _scopes = {"core"}

    def __init__(self, sessions: "SessionManager") -> None:
        self._sessions = sessions
        self._request_ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sessions = getattr(ctx, "sessions", None)
        assert sessions is not None  # guarded by enabled()
        return cls(sessions=sessions)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "session_search"

    @property
    def description(self) -> str:
        return (
            "Search this conversation's prior messages for a keyword or "
            "regex. Use when you need to recall a specific value, file "
            "path, error, or decision from earlier in the session and "
            "re-reading the full history would be wasteful. Covers the "
            "live history AND older history trimmed from the transcript "
            "into the session archive, so matches may predate anything "
            "still visible. Returns matches with their message index "
            "(or [archived <time>] for archived ones), role, and a short "
            "surrounding snippet. Searches only this session — for "
            "cross-session lookups, use memory tools instead."
        )

    def _session(self) -> Any | None:
        if self._request_ctx is None:
            return None
        key = self._request_ctx.session_key
        if not key:
            return None
        return self._sessions.get_or_create(key)

    async def execute(
        self,
        query: str | None = None,
        regex: bool | None = None,
        case_sensitive: bool | None = None,
        role: str | None = None,
        max_results: int | None = None,
        snippet_chars: int | None = None,
        **kwargs: Any,
    ) -> str:
        if not query or not str(query).strip():
            return "Error: `query` is required and must be non-empty."
        query_str = str(query)
        use_regex = bool(regex)
        case_sensitive_flag = bool(case_sensitive)
        if role is not None and role not in _ALLOWED_ROLES:
            return (
                f"Error: `role` must be one of {sorted(_ALLOWED_ROLES)} "
                f"or omitted (got {role!r})."
            )
        cap = max_results if max_results is not None else _DEFAULT_MAX_RESULTS
        cap = max(1, min(int(cap), _MAX_RESULTS_CAP))
        width = snippet_chars if snippet_chars is not None else _DEFAULT_SNIPPET_CHARS
        width = max(_MIN_SNIPPET_CHARS, min(int(width), _MAX_SNIPPET_CHARS))

        session = self._session()
        if session is None or not getattr(session, "messages", None):
            return "No prior messages in this session to search."

        # Compile the pattern. Keyword mode uses re.escape; both modes
        # honor case_sensitive. Invalid regex surfaces as a clear tool
        # error rather than a Python traceback.
        flags = 0 if case_sensitive_flag else re.IGNORECASE
        try:
            pattern_src = query_str if use_regex else re.escape(query_str)
            pattern = re.compile(pattern_src, flags)
        except re.error as exc:
            return f"Error: invalid regex pattern: {exc}"

        matches: list[dict[str, Any]] = []
        for idx, msg in enumerate(session.messages):
            if not isinstance(msg, dict):
                continue
            entry = _match_message(msg, pattern, role, width)
            if entry is None:
                continue
            entry["msg_index"] = idx
            matches.append(entry)

        # Continue into the archive segments (prefixes the file cap trimmed
        # off the live file) only when the live scan left room under the cap
        # — live matches are newer and would displace archived ones anyway.
        archived: list[dict[str, Any]] = []
        archive_scanned = 0
        archive_complete = True
        key = self._request_ctx.session_key if self._request_ctx else None
        if key and len(matches) < cap:
            paths = self._sessions.archive_paths(key)
            if paths:
                archived, archive_scanned, archive_complete = await asyncio.to_thread(
                    _scan_archive,
                    paths,
                    pattern,
                    role,
                    cap - len(matches),
                    width,
                    _ARCHIVE_SCAN_MAX_BYTES,
                )

        combined = archived + matches  # archive is strictly older → first
        total = len(combined)
        if total == 0:
            out = f"No matches for {query_str!r} in {len(session.messages)} messages."
            if archive_scanned:
                out += (
                    f" Archive: {archive_scanned} segment(s) scanned, no matches"
                    + ("" if archive_complete else " (partial scan)")
                    + "."
                )
            return out

        # Keep the last `cap` matches — chronological tail is usually the
        # most relevant context for "what did I see most recently?".
        shown = combined[-cap:] if total > cap else combined

        header = (
            f"{total} match{'es' if total != 1 else ''} for {query_str!r} "
            f"across {len(session.messages)} messages"
        )
        if archived or archive_scanned:
            header += (
                f" + archive ({len(archived)} archived match"
                f"{'es' if len(archived) != 1 else ''}"
                + ("" if archive_complete else ", partial scan")
                + ")"
            )
        if total > cap:
            header += f" (showing last {cap})"
        header += ":"
        body_lines: list[str] = []
        for entry in shown:
            role_label = entry["role"]
            if "tool" in entry and entry["role"] == "tool":
                role_label = f"tool({entry['tool']})"
            if entry.get("archived"):
                ts = entry.get("ts") or ""
                ref = f"[archived {ts}]" if ts else "[archived]"
            else:
                ref = f"[{entry['msg_index']}]"
            body_lines.append(f"  {ref} {role_label}: {entry['snippet']}")

        out = header + "\n" + "\n".join(body_lines)
        if len(out) > _TOTAL_OUTPUT_BUDGET:
            out = (
                out[: _TOTAL_OUTPUT_BUDGET - 80].rstrip()
                + "\n… (output truncated; narrow your query or "
                "lower `snippet_chars` for more results.)"
            )
        return out
