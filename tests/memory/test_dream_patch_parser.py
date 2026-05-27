"""Parser for the v2 Dream consolidator output format.

Per `docs/memory/05_dream_cold_path.md` §6 + `docs/memory/06_prompts_and_instructions.md` §4:

Sections, in order, terminated by `===END===`:
  ===PATCH===
  <JSON array of patch ops>
  ===BODY_DELTA===
  <markdown text, possibly empty>
  ===COMMIT===
  <subject + body + trailers>
  ===END===

JSON tolerance: `json_repair` salvages near-valid output (trailing
commas, missing brackets, smart quotes, etc.). Hard failure → return
None.
"""

from __future__ import annotations

import pytest

from durin.memory.dream_patch_parser import (
    DreamPatchParseError,
    ParsedDreamOutput,
    parse_dream_output,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parses_full_well_formed_output() -> None:
    text = """\
===PATCH===
[
  {"op": "add", "path": "/attributes/email",
   "value": "m@x.com", "provenance": "episodic/foo.md"}
]
===BODY_DELTA===
Marcelo updated his email.
===COMMIT===
Update Marcelo's email

The May 26 standup confirmed the new work email.

Sources: episodic/foo.md
Cursor-after: 2026-05-26T08:45:00Z
Entities-touched: person:marcelo
===END===
"""
    out = parse_dream_output(text)
    assert isinstance(out, ParsedDreamOutput)
    assert len(out.patch_ops) == 1
    assert out.patch_ops[0]["op"] == "add"
    assert out.patch_ops[0]["provenance"] == "episodic/foo.md"
    assert out.body_delta == "Marcelo updated his email."
    assert "Update Marcelo's email" in out.commit_message
    assert "Sources: episodic/foo.md" in out.commit_message


def test_empty_patch_array_is_valid() -> None:
    """Rule 8: when no ops are warranted, an empty patch is a valid
    successful pass."""
    text = """\
===PATCH===
[]
===BODY_DELTA===
===COMMIT===
No-op pass — pending entries re-affirm canonical

Sources: episodic/a.md, episodic/b.md
Cursor-after: 2026-05-29T12:15:00Z
Entities-touched: person:marcelo
===END===
"""
    out = parse_dream_output(text)
    assert out.patch_ops == []
    assert out.body_delta == ""


def test_empty_body_delta_is_valid() -> None:
    text = """\
===PATCH===
[{"op": "add", "path": "/aliases/-", "value": "m", "provenance": "x.md"}]
===BODY_DELTA===

===COMMIT===
Add alias m

Sources: x.md
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:marcelo
===END===
"""
    out = parse_dream_output(text)
    assert out.body_delta == ""
    assert len(out.patch_ops) == 1


def test_multiline_body_delta_preserved() -> None:
    text = """\
===PATCH===
[]
===BODY_DELTA===
Line 1.

Line 2.

Line 3.
===COMMIT===
S

Sources: x.md
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:marcelo
===END===
"""
    out = parse_dream_output(text)
    assert out.body_delta == "Line 1.\n\nLine 2.\n\nLine 3."


# ---------------------------------------------------------------------------
# json_repair tolerance — common small-model quirks
# ---------------------------------------------------------------------------


def test_trailing_comma_in_patch_repaired() -> None:
    text = """\
===PATCH===
[
  {"op": "add", "path": "/attributes/x", "value": 1, "provenance": "p",}
]
===BODY_DELTA===
===COMMIT===
S

Sources: p
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:x
===END===
"""
    out = parse_dream_output(text)
    assert out.patch_ops[0]["op"] == "add"


def test_missing_closing_bracket_repaired() -> None:
    text = """\
===PATCH===
[
  {"op": "add", "path": "/attributes/x", "value": 1, "provenance": "p"}
===BODY_DELTA===
===COMMIT===
S

Sources: p
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:x
===END===
"""
    out = parse_dream_output(text)
    assert out.patch_ops[0]["op"] == "add"


def test_code_fence_around_patch_stripped() -> None:
    """Small models sometimes wrap the patch in ```json fences."""
    text = """\
===PATCH===
```json
[{"op": "add", "path": "/attributes/x", "value": 1, "provenance": "p"}]
```
===BODY_DELTA===
===COMMIT===
S

Sources: p
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:x
===END===
"""
    out = parse_dream_output(text)
    assert len(out.patch_ops) == 1
    assert out.patch_ops[0]["op"] == "add"


# ---------------------------------------------------------------------------
# Hard failures — raise DreamPatchParseError
# ---------------------------------------------------------------------------


def test_missing_patch_marker_raises() -> None:
    text = "===BODY_DELTA===\n\n===COMMIT===\nfoo\n===END===\n"
    with pytest.raises(DreamPatchParseError):
        parse_dream_output(text)


def test_missing_commit_marker_raises() -> None:
    text = "===PATCH===\n[]\n===BODY_DELTA===\n\n===END===\n"
    with pytest.raises(DreamPatchParseError):
        parse_dream_output(text)


def test_missing_end_marker_raises() -> None:
    text = (
        "===PATCH===\n[]\n===BODY_DELTA===\n\n===COMMIT===\nfoo\n"
    )
    with pytest.raises(DreamPatchParseError):
        parse_dream_output(text)


def test_patch_not_a_list_raises() -> None:
    text = """\
===PATCH===
{"op": "add"}
===BODY_DELTA===
===COMMIT===
S

Sources: p
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:x
===END===
"""
    with pytest.raises(DreamPatchParseError):
        parse_dream_output(text)


def test_unrepairable_patch_raises() -> None:
    text = """\
===PATCH===
this is not json at all, just prose
===BODY_DELTA===
===COMMIT===
S

Sources: p
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:x
===END===
"""
    with pytest.raises(DreamPatchParseError):
        parse_dream_output(text)


# ---------------------------------------------------------------------------
# Op-level shape sanity (the parser does NOT validate semantics; that's
# the applier's job. But malformed op objects — missing required keys —
# surface clearly so callers can short-circuit.)
# ---------------------------------------------------------------------------


def test_op_missing_provenance_still_parses() -> None:
    """The parser does not enforce provenance. The applier does. That
    way the applier can produce a precise telemetry event."""
    text = """\
===PATCH===
[{"op": "add", "path": "/attributes/x", "value": 1}]
===BODY_DELTA===
===COMMIT===
S

Sources: p
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:x
===END===
"""
    out = parse_dream_output(text)
    assert "provenance" not in out.patch_ops[0]


# ---------------------------------------------------------------------------
# Whitespace tolerance — extra blank lines between markers
# ---------------------------------------------------------------------------


def test_extra_blank_lines_around_markers_tolerated() -> None:
    text = """\

===PATCH===

[]


===BODY_DELTA===


===COMMIT===

Subject

Sources: p
Cursor-after: 2026-05-26T00:00:00Z
Entities-touched: person:x

===END===

"""
    out = parse_dream_output(text)
    assert out.patch_ops == []
    assert "Subject" in out.commit_message
