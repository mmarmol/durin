"""The pass/fail contract for a routing agent node: it ends its reply with a
verdict line, parsed here. Default FAIL so an unparseable answer loops back
rather than silently passing.

Multi-way routing uses parse_label instead: the agent ends its reply with one
of the declared case labels; the last matching line wins."""
from __future__ import annotations

import re
from typing import Iterable

_PUNCT = re.compile(r"^[^\w]+|[^\w]+$")
_LEADING_PUNCT = re.compile(r"^[^\w]+")
# Lines that are part of something else, never the node's own verdict: a list
# item (a checklist, a fix list), a quote, a table row, and a fence line.
_LIST_ITEM = re.compile(r"(?:[-*+•]|\d+[.)])\s")
_FENCE = re.compile(r" {0,3}(?:```|~~~)")
# "Verdict: FAIL", "**Verdict:** PASS", "Overall: FAIL — …", "Final verdict: PASS".
_LABEL = re.compile(
    r"(?i:(?:final\s+|overall\s+)?(?:verdict|result|decision|outcome|overall))\b[^\w\n]*")
# Only the uppercase words state a verdict. A FAIL line is FAIL (or FAILED) as
# its first word, followed by anything: "FAIL" and what to fix is the verdict's
# own form. A PASS line is PASS alone, set off by punctuation or emphasis
# ("PASS.", "**PASS** …", "PASS — caveats", "PASS (…)"), or "PASS with … caveats";
# PASS followed straight by words is not read as a verdict line.
_FAIL_LINE = re.compile(r"FAIL(?:ED)?(?![\w-])")
_PASS_LINE = re.compile(r"PASS(?![\w-])(?:$|[*`]|\s*[^\w\s]|\s+with\b.*\bcaveats?\b)")


def normalize_label(s: str) -> str:
    """Strip leading/trailing punctuation and uppercase.

    Used both when matching agent output and when validating that declared case
    labels are distinct — two labels that normalize to the same form would cause
    a silent mis-route, so the spec rejects them at parse time.
    """
    return _PUNCT.sub("", s).upper()


def _statement_lines(lines: list[str]):
    """``(index, line)`` for each line that could state the node's own verdict:
    not inside a fenced or indented code block, and not a list item, a quote or
    a table row."""
    in_fence = False
    for i, raw in enumerate(lines):
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence or raw.startswith(("    ", "\t")):
            continue
        s = raw.strip()
        if not s or s.startswith((">", "|")) or _LIST_ITEM.match(s):
            continue
        yield i, s


def _after_label(line: str) -> str:
    """*line* without its leading markdown and ``Verdict:``-style label."""
    s = _LEADING_PUNCT.sub("", line.strip())
    label = _LABEL.match(s)
    return s[label.end():] if label else s


def _verdict_of(line: str) -> bool | None:
    """True for a line that states PASS, False for FAIL, None otherwise (see
    ``_PASS_LINE`` / ``_FAIL_LINE``)."""
    s = _after_label(line)
    if _FAIL_LINE.match(s):
        return False
    if _PASS_LINE.match(s):
        return True
    return None


def _verdict_lines(lines: list[str]) -> list[tuple[int, bool]]:
    """``(index, passed)`` for every line of *lines* that states a verdict."""
    found = []
    for i, line in _statement_lines(lines):
        verdict = _verdict_of(line)
        if verdict is not None:
            found.append((i, verdict))
    return found


def parse_verdict(text: str) -> bool:
    """Return True iff the lines of *text* that state a verdict all say PASS.

    Any FAIL line beside a PASS reads as FAIL, whichever comes last, so a
    disagreement only costs another pass. Lines that are something else — code,
    list items such as a checklist or a fix list, quotes, tables — are not read.
    When no line states a verdict, the first non-empty line is read as a leading
    verdict, PASS when it starts with 'PASS' in any case, so prompts that put
    the verdict first (even with words after it on that line) still route.
    False (FAIL) otherwise."""
    lines = (text or "").splitlines()
    found = _verdict_lines(lines)
    if found:
        return all(passed for _, passed in found)
    first = next((line.strip() for line in lines if line.strip()), "")
    return first.upper().startswith("PASS")


def parse_label(text: str, labels: Iterable[str]) -> str | None:
    """Return the label (from *labels*) that the last matching non-empty line equals.

    Matching is case-insensitive and tolerates surrounding punctuation/whitespace
    on the line (e.g. "GROUNDED." or "**missing**" match "GROUNDED" and "MISSING").
    The whole stripped, de-punctuated line must equal the label exactly — a label
    that is a substring of a longer word does not match.

    Scans lines from the end; returns the original label (preserving its case) from
    the *labels* iterable on the first match, or None if no line matches any label.
    """
    # Build a lookup: normalized form -> original label (last one wins for duplicates).
    label_map: dict[str, str] = {}
    for label in labels:
        norm = normalize_label(label)
        if norm:
            label_map[norm] = label

    lines = (text or "").splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        norm_line = normalize_label(stripped)
        if norm_line in label_map:
            return label_map[norm_line]
    return None


def strip_verdict_line(text: str, passed: bool | None = None) -> str:
    """The binary routing node's output minus a line that is nothing but its
    verdict — what the node said BESIDES the verdict. Used when a gate ends the
    run, so a terminal gate that produced real content (a verification summary,
    a final answer) contributes it instead of the run returning a stale upstream
    output, and a gate that answered only "PASS" leaves that output in place.

    Only the last non-empty line, else the first (a verdict put first), is
    removed, and only when it is the bare verdict (a ``Verdict:``-style label
    and surrounding punctuation allowed); with *passed* — the verdict the engine
    routed on — only when it agrees. Any other line is content, and a closing
    verdict that disagrees with a `route` call's decision is information."""
    lines = (text or "").splitlines()
    filled = [i for i, line in enumerate(lines) if line.strip()]
    for i in filled[-1:] + filled[:1]:
        word = normalize_label(_after_label(lines[i]))
        if word in ("PASS", "FAIL") and (passed is None or (word == "PASS") is passed):
            return "\n".join(lines[:i] + lines[i + 1:]).strip()
    return (text or "").strip()


def strip_label_line(text: str, labels: Iterable[str]) -> str:
    """The multi-way node's output minus its trailing case-label line (same
    normalization as parse_label). See strip_verdict_line for why."""
    norms = {normalize_label(label) for label in labels}
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if not s:
            continue
        if normalize_label(s) in norms:
            return "\n".join(lines[:i] + lines[i + 1:]).strip()
        break
    return (text or "").strip()
