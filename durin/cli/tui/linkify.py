"""Wrap URLs and file paths with OSC 8 hyperlinks via Rich markup.

When a result text from a tool (or any plain text bubble) contains a
URL or an absolute path, we want the user to be able to ``Cmd+click``
to open it in their browser or file manager. We don't need to handle
the click ourselves: modern terminals (iTerm2, WezTerm, Kitty, recent
macOS Terminal.app, Windows Terminal) interpret OSC 8 escape sequences
as clickable hyperlinks, and Rich emits OSC 8 for any text wrapped in
``[link=URI]...[/link]``.

So our job here is just to *detect* the URLs and paths in plain text
and wrap them. Detection rules are intentionally conservative:

- URLs match ``http(s)://``-prefixed strings.
- Absolute paths must start with ``/`` or ``~/`` and contain at least
  one further path separator (rules out lone slashes / single tokens).

That keeps us from accidentally linkifying random text — relative
paths and tokens that look path-like are left alone.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from rich.text import Text

__all__ = [
    "linkify",
    "linkify_to_text",
    "autolinkify_markdown",
    "URL_RE",
    "PATH_RE",
]


# Stops at whitespace and the usual sentence-ending punctuation that
# almost never belongs inside a URL.
URL_RE = re.compile(
    r"https?://[^\s\)\]\}>\"'`]+"
)

# Absolute path: leading `/` or `~/`, then at least one alphanumeric
# (so `//` or `/ ` don't match), then path-y characters. Negative
# lookbehind avoids gobbling the `://` part of an already-matched URL.
PATH_RE = re.compile(
    r"(?<![:/])(?P<p>(?:/|~/)[\w][\w.~\-+/@]*)"
)


def linkify(text: str) -> Text:
    """Return a Rich :class:`Text` with all URLs / abs paths wrapped in OSC 8.

    Idempotent — calling on an already-linkified Text would no-op (we
    only consume plain strings; styled segments inside the source are
    not produced by this function, so re-linkification just rebuilds
    the same output).
    """
    if not text:
        return Text("")

    # Build a single ordered list of (start, end, kind, render) spans
    # so URL + PATH matches don't conflict when one contains the other.
    spans: list[tuple[int, int, str]] = []
    for m in URL_RE.finditer(text):
        spans.append((m.start(), m.end(), m.group(0)))
    for m in PATH_RE.finditer(text):
        # Skip a match that lives inside a URL we already captured.
        if _overlaps_any(m.start(), m.end(), spans):
            continue
        spans.append((m.start(), m.end(), m.group("p")))
    spans.sort(key=lambda s: s[0])

    if not spans:
        return Text(text)

    out = Text()
    cursor = 0
    for start, end, raw in spans:
        if cursor < start:
            out.append(text[cursor:start])
        uri = _to_uri(raw)
        # Rich wraps the link target in OSC 8 escape codes; the visible
        # text is unchanged so the layout doesn't shift.
        out.append(raw, style=f"underline link {uri}")
        cursor = end
    if cursor < len(text):
        out.append(text[cursor:])
    return out


def autolinkify_markdown(text: str) -> str:
    """Convert bare URLs / abs paths in Markdown source to ``[url](url)`` links.

    Rich's :class:`rich.markdown.Markdown` renders ``[text](url)`` as a
    clickable hyperlink (OSC 8) but leaves bare URLs as plain text. To
    keep assistant replies clickable we pre-process the markdown source
    and convert bare URLs / paths to explicit link syntax — but we
    skip:

    - URLs that are already inside ``[...](...)`` (no double-wrap)
    - URLs inside fenced code blocks (``` ... ```)
    - URLs inside inline code spans (``...``)

    This is a best-effort pre-pass; it doesn't aim to handle every
    Markdown corner case (e.g. autolinks delimited by ``<>``), just
    the common chat-output shapes our agent emits.
    """
    if not text:
        return text

    out_parts: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        # Track fenced code-block state. The fence delimiter is ``` (or
        # ~~~), possibly with a language tag right after.
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_parts.append(line)
            continue
        if in_fence:
            out_parts.append(line)
            continue
        out_parts.append(_autolinkify_line(line))
    return "\n".join(out_parts)


# Inline code spans wrapped in single backticks: `…`. We split on these
# so we can leave their contents untouched. Captures the backticks too
# so re-joining preserves them.
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# Already-linked markdown: `[anchor text](target)`. We treat the WHOLE
# anchor+target as opaque — don't re-substitute inside.
_MD_LINK_RE = re.compile(r"\[[^\]\n]+\]\([^\)\n]+\)")


def _autolinkify_line(line: str) -> str:
    """Apply auto-linking on one non-fenced markdown line."""
    # Split on already-wrapped links so we don't double-wrap.
    pieces: list[str] = []
    cursor = 0
    for m in _MD_LINK_RE.finditer(line):
        pieces.append(_autolinkify_outside_links(line[cursor:m.start()]))
        pieces.append(m.group(0))  # keep existing link verbatim
        cursor = m.end()
    pieces.append(_autolinkify_outside_links(line[cursor:]))
    return "".join(pieces)


def _autolinkify_outside_links(segment: str) -> str:
    """Auto-link URLs / paths in a segment that has no markdown links."""
    if not segment:
        return segment
    # Skip inline code spans by splitting + recombining.
    pieces: list[str] = []
    cursor = 0
    for m in _INLINE_CODE_RE.finditer(segment):
        pieces.append(_substitute_bare(segment[cursor:m.start()]))
        pieces.append(m.group(0))  # keep code verbatim
        cursor = m.end()
    pieces.append(_substitute_bare(segment[cursor:]))
    return "".join(pieces)


def _substitute_bare(text: str) -> str:
    """Replace bare URLs / paths with ``[url](url)`` Markdown links."""
    if not text:
        return text
    # Run URLs first to claim them before PATH_RE might match a slash
    # inside the URL.
    out: str = URL_RE.sub(lambda m: f"[{m.group(0)}]({m.group(0)})", text)
    # Paths are subtler: we only want `/abs/path` or `~/abs/path` —
    # PATH_RE already enforces that. Negative lookbehind for `:/`
    # prevents catching a path INSIDE a URL we just wrapped.
    out = PATH_RE.sub(_wrap_path_md, out)
    return out


def _wrap_path_md(m: re.Match[str]) -> str:
    raw = m.group("p")
    target = _to_uri(raw)
    return f"[{raw}]({target})"


def linkify_to_text(text: str | Text) -> Text:
    """Convenience: accept either a plain string or an existing :class:`Text`.

    For an existing ``Text`` we copy spans across — Rich's ``Text`` is
    mutable enough that re-linkifying would risk double-wrapping, so
    callers that already produced styled text should not re-call this.
    """
    if isinstance(text, Text):
        return text
    return linkify(str(text or ""))


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int, str]]) -> bool:
    for s, e, _ in spans:
        if start < e and s < end:
            return True
    return False


def _to_uri(raw: str) -> str:
    """Convert a detected URL or path into an OSC 8 target URI.

    Paths are NOT resolved (no symlink-following) so `/tmp/x` stays
    `/tmp/x` instead of becoming `/private/tmp/x` on macOS.
    """
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    return f"file://{raw}"
