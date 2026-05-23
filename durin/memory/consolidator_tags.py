"""Parse the consolidator LLM response into (summary, tags).

The consolidator prompt (``templates/agent/consolidator_archive.md``) is
extended in Phase 1.4 to emit a trailing YAML block with two fields:

    entities: [...]
    topics: [...]

separated from the bullet-point summary by a ``---`` line. This module
splits the response back into the summary text (just the bullets) and a
tags dict that gets persisted to ``meta.json::derived.tags``.

The parser is intentionally lenient: any failure mode (no tags block,
malformed YAML, wrong shape) returns the raw response as the summary
and an empty tags dict. Callers must never blow up on a degraded LLM
response — the worst outcome is "no tags this turn".
"""

from __future__ import annotations

from typing import Any

import yaml

__all__ = ["parse_consolidator_response"]

_SEPARATOR = "\n---\n"


def parse_consolidator_response(raw: str) -> tuple[str, dict[str, list[str]]]:
    """Split a consolidator response into (summary_text, tags_dict).

    Tags dict shape: ``{"entities": [...], "topics": [...]}``. Missing
    keys are filled with empty lists. Any parse failure returns the raw
    input as the summary and ``{"entities": [], "topics": []}``.
    """
    empty_tags: dict[str, list[str]] = {"entities": [], "topics": []}

    if not isinstance(raw, str) or not raw.strip():
        return raw, empty_tags

    if raw.strip() == "(nothing)":
        return raw, empty_tags

    if _SEPARATOR not in raw:
        return raw, empty_tags

    head, _, tail = raw.rpartition(_SEPARATOR)
    summary_text = head.rstrip()
    tags_block = tail.strip()
    if not tags_block:
        return summary_text, empty_tags

    try:
        parsed: Any = yaml.safe_load(tags_block)
    except yaml.YAMLError:
        return summary_text, empty_tags

    if not isinstance(parsed, dict):
        return summary_text, empty_tags

    # Lenient entity validation on the read path (per doc 14 §3.2):
    # drop malformed refs silently — a degraded LLM output should never
    # break consolidation. Valid refs flow through; invalid ones are
    # dropped on the floor.
    entities = _coerce_string_list(parsed.get("entities"))
    from durin.memory.entities import is_valid_entity_ref

    entities = [e for e in entities if is_valid_entity_ref(e)]

    return summary_text, {
        "entities": entities,
        "topics": _coerce_string_list(parsed.get("topics")),
    }


def _coerce_string_list(value: Any) -> list[str]:
    """Force a value into a list[str], dropping empties; return [] on failure."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out
