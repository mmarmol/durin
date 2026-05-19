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
  not read from the on-disk jsonl, which would be redundant and could
  drift mid-turn while messages are being appended.
- Both keyword (substring) and regex modes are supported. Keyword mode
  is the default — it is forgiving and fast; regex is opt-in for power
  users / chain-of-tool flows.
- The tool only exposes content the model has already produced or seen.
  No new data-leak surface vs. the existing history-rendering path.
- Allowed in every agent mode (read-only).
"""

from __future__ import annotations

import re
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
            "re-reading the full history would be wasteful. Returns "
            "matches with their message index, role, and a short "
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
            msg_role = msg.get("role")
            if role is not None and msg_role != role:
                continue
            text = _extract_text(msg.get("content"))
            if not text:
                continue
            m = pattern.search(text)
            if m is None:
                continue
            snippet = _make_snippet(text, m.start(), m.end(), width)
            entry: dict[str, Any] = {
                "msg_index": idx,
                "role": msg_role or "?",
                "snippet": snippet,
            }
            # Tool messages carry the tool name in the `name` field —
            # surfacing it helps the model decide whether the snippet
            # is from a useful source.
            if msg_role == "tool":
                tool_name = msg.get("name")
                if isinstance(tool_name, str) and tool_name:
                    entry["tool"] = tool_name
            matches.append(entry)

        total = len(matches)
        if total == 0:
            return f"No matches for {query_str!r} in {len(session.messages)} messages."

        # Keep the last `cap` matches — chronological tail is usually the
        # most relevant context for "what did I see most recently?".
        shown = matches[-cap:] if total > cap else matches

        header = (
            f"{total} match{'es' if total != 1 else ''} for {query_str!r} "
            f"across {len(session.messages)} messages"
        )
        if total > cap:
            header += f" (showing last {cap})"
        header += ":"
        body_lines: list[str] = []
        for entry in shown:
            role_label = entry["role"]
            if "tool" in entry and entry["role"] == "tool":
                role_label = f"tool({entry['tool']})"
            body_lines.append(
                f"  [{entry['msg_index']}] {role_label}: {entry['snippet']}"
            )

        out = header + "\n" + "\n".join(body_lines)
        if len(out) > _TOTAL_OUTPUT_BUDGET:
            out = (
                out[: _TOTAL_OUTPUT_BUDGET - 80].rstrip()
                + "\n… (output truncated; narrow your query or "
                "lower `snippet_chars` for more results.)"
            )
        return out
