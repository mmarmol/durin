"""Parser for the v2 Dream consolidator output format.

The LLM emits four sections separated by `===<NAME>===` markers and
terminated by `===END===`:

    ===PATCH===
    <JSON array of RFC 6902 patch ops with extra `provenance` field>
    ===BODY_DELTA===
    <markdown text appended to the entity page body, or empty>
    ===COMMIT===
    <subject + body + trailers — see `commit_format.md`>
    ===END===

This module's job is **structural**: split the markers, recover the
PATCH JSON (using ``json_repair`` to tolerate small-model quirks like
trailing commas, missing brackets, and ```` ```json ```` code fences),
and return a typed result. Semantic validation (paths inside allowed
roots, every op carries `provenance`, etc.) is the *applier's* job,
performed against the parsed result.

Why a separate module: keeps the parser independently testable, lets
the applier produce precise telemetry (`parse_failed` vs
`validation_failed`), and lets us swap parsing strategies (e.g.
constrained-output via tool-use) without touching the rest of the
Dream pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from json_repair import repair_json

__all__ = [
    "DreamPatchParseError",
    "ParsedDreamOutput",
    "parse_dream_output",
]


class DreamPatchParseError(Exception):
    """The LLM output could not be structurally parsed. The applier
    treats this as a `parse_failed` outcome and does NOT advance the
    cursor."""


@dataclass(frozen=True)
class ParsedDreamOutput:
    """One Dream LLM response broken into its three semantic parts."""

    patch_ops: list[dict[str, Any]] = field(default_factory=list)
    body_delta: str = ""
    commit_message: str = ""


# Each marker line must appear on its own line (per spec §5.2 prompt
# template). We anchor on `\n===NAME===\n` so partial matches inside
# the body delta (where the LLM might echo a marker as prose) don't
# split prematurely.
_MARKERS = ("PATCH", "BODY_DELTA", "COMMIT", "END")
_MARKER_RE = re.compile(
    r"(?m)^\s*===(PATCH|BODY_DELTA|COMMIT|END)===\s*$"
)

# Code-fence variants the LLM sometimes wraps around the patch payload.
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n(.*?)\n```\s*$",
    re.DOTALL,
)


def parse_dream_output(text: str) -> ParsedDreamOutput:
    """Parse a raw Dream LLM response into its three sections.

    Raises :class:`DreamPatchParseError` for unrecoverable shapes:
    missing markers, wrong marker order, patch payload that isn't a
    JSON array even after ``json_repair``.
    """
    if not isinstance(text, str):
        raise DreamPatchParseError("LLM response is not a string")

    sections = _split_sections(text)
    if "PATCH" not in sections:
        raise DreamPatchParseError("missing ===PATCH=== marker")
    if "COMMIT" not in sections:
        raise DreamPatchParseError("missing ===COMMIT=== marker")
    if "END" not in sections:
        raise DreamPatchParseError("missing ===END=== marker")

    patch_raw = sections["PATCH"]
    body_delta = sections.get("BODY_DELTA", "").strip("\n").rstrip()
    commit_message = sections["COMMIT"].strip("\n").rstrip()

    patch_ops = _parse_patch_payload(patch_raw)
    return ParsedDreamOutput(
        patch_ops=patch_ops,
        body_delta=body_delta,
        commit_message=commit_message,
    )


def _split_sections(text: str) -> dict[str, str]:
    """Return a map from marker name to the raw text under it.

    The text under a marker runs from the line AFTER its marker line
    up to (but not including) the next marker line. ``===END===`` has
    no text under it; we still record its presence so the caller can
    enforce the terminator.
    """
    # Iterate match objects to capture positions.
    matches = list(_MARKER_RE.finditer(text))
    if not matches:
        raise DreamPatchParseError(
            "no ===NAME=== markers found in LLM response"
        )

    # Enforce expected order: PATCH → BODY_DELTA (optional) → COMMIT → END.
    names = [m.group(1) for m in matches]
    # PATCH must come first.
    if names[0] != "PATCH":
        raise DreamPatchParseError(
            f"first marker is {names[0]!r}, expected 'PATCH'"
        )

    # Collect section text per marker. We accept BODY_DELTA being
    # absent (some models skip it on empty); §6 of the spec shows it
    # required, but lenient handling avoids spurious rejects.
    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        name = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[start:end]
    return sections


def _parse_patch_payload(raw: str) -> list[dict[str, Any]]:
    """Recover a JSON array of ops from the raw PATCH section text.

    Steps:
      1. Strip ```` ```json ```` code fences if present.
      2. Strip outer whitespace.
      3. Run through ``json_repair`` to salvage trailing commas, smart
         quotes, missing brackets.
      4. Validate it's a list of dicts.
    """
    payload = raw.strip()
    fence_match = _CODE_FENCE_RE.match(payload)
    if fence_match:
        payload = fence_match.group(1).strip()
    if not payload:
        raise DreamPatchParseError("===PATCH=== section is empty")
    try:
        repaired = repair_json(payload, return_objects=True)
    except (ValueError, TypeError) as exc:
        raise DreamPatchParseError(
            f"json_repair could not salvage PATCH payload: {exc}"
        ) from exc

    # json_repair returns "" or a non-container on total failure; the
    # empty-string case is the most common.
    if repaired == "" or repaired is None:
        raise DreamPatchParseError(
            "json_repair returned empty result — PATCH payload is "
            "not recoverable as JSON"
        )

    if not isinstance(repaired, list):
        raise DreamPatchParseError(
            f"PATCH payload must be a JSON array, got "
            f"{type(repaired).__name__}"
        )
    # Each op should be a dict; we don't enforce required keys here
    # (the applier does, with op-level provenance + path validation).
    ops: list[dict[str, Any]] = []
    for i, item in enumerate(repaired):
        if not isinstance(item, dict):
            raise DreamPatchParseError(
                f"PATCH op #{i} is {type(item).__name__}, expected object"
            )
        ops.append(item)
    return ops
