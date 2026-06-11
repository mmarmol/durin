"""Search-output dedup against the hot layer (P4, 2026-06-10).

``memory_search`` hits whose rendered content is already visible in the
caller's system prompt — the hot layer's ``=== CANONICAL ===`` /
``=== FRAGMENT ===`` blocks — are pure token waste: the model pays for
the same text twice in the turn it lands, then re-reads it on every
subsequent turn of the session as part of replayed history.

The dedup is containment-based and false-negative-safe: a hit is
"already in context" only when its rendered body (the exact text
``sectioned_output._render_block`` would print, ``summary > body >
snippet``) is a whitespace-normalised substring of the hot-layer block
for the SAME ref. A hit that carries anything beyond what the prefix
shows passes through untouched. Redundant hits are not dropped — they
surface as pointer lines (uri + ts) so the model keeps citation refs
and can ``memory_drill`` for the full body.

Callers whose system prompt does NOT carry the hot layer (subagents —
see ``SubagentManager._build_subagent_prompt``) must skip this dedup
entirely; ``MemorySearchTool`` gates it on ``ToolContext.scope``.
"""

from __future__ import annotations

import re
from pathlib import Path

from durin.memory.hot_layer import HotLayer, read_hot_layer
from durin.memory.sectioned_output import SectionedHit

__all__ = [
    "prefix_map",
    "render_in_context_section",
    "split_in_context",
]

_WS = re.compile(r"\s+")

# Hit types that can appear in the hot layer. Skills / sessions /
# ingested chunks never surface there, so they are never deduped.
_CANONICAL_TYPES = ("entity",)
_FRAGMENT_TYPES = ("episodic", "stable")


def _norm(text: str) -> str:
    """Whitespace-collapse + casefold so containment survives wrapping."""
    return _WS.sub(" ", text).strip().casefold()


def _parse_block(block: str, kind: str) -> tuple[str, str] | None:
    """Parse a hot-layer block into ``(key, body_text)``.

    The header shape is single-sourced in ``section_markers``:
    ``=== CANONICAL: <ref> (consolidated <ts>) ===`` /
    ``=== FRAGMENT: <path> (ts <ts>) ===``. The qualifier parens are
    stripped with a right-split so refs/paths themselves stay intact.
    """
    first, _, rest = block.partition("\n")
    prefix = f"=== {kind}: "
    if not first.startswith(prefix):
        return None
    header = first[len(prefix):]
    if header.endswith(" ==="):
        header = header[: -len(" ===")]
    key = header.rsplit(" (", 1)[0].strip()
    if not key:
        return None
    return key, rest


def prefix_map(hot: HotLayer) -> dict[str, str]:
    """Map hot-layer block keys to their normalised body text.

    Canonical blocks key by entity ref (``person:marcelo``); fragment
    blocks key by entry path with the ``.md`` suffix stripped so the
    key matches the ``memory/<class>/<id>`` uri shape search hits carry.
    """
    out: dict[str, str] = {}
    for block in hot.canonical_blocks:
        parsed = _parse_block(block, "CANONICAL")
        if parsed is not None:
            out[parsed[0]] = _norm(parsed[1])
    for block in hot.fragment_blocks:
        parsed = _parse_block(block, "FRAGMENT")
        if parsed is not None:
            key = parsed[0]
            if key.endswith(".md"):
                key = key[: -len(".md")]
            out[key] = _norm(parsed[1])
    return out


def _hit_key(hit: SectionedHit) -> str | None:
    """Normalise a hit's uri to the prefix-map key shape, or None."""
    if hit.type in _CANONICAL_TYPES:
        uri = hit.uri
        if uri.startswith("memory/entity_page/"):
            uri = uri[len("memory/entity_page/"):]
        return uri
    if hit.type in _FRAGMENT_TYPES:
        uri = hit.uri
        if uri.endswith(".md"):
            uri = uri[: -len(".md")]
        return uri
    return None


def split_in_context(
    workspace: Path, hits: list[SectionedHit],
) -> tuple[list[SectionedHit], list[SectionedHit]]:
    """Partition ``hits`` into ``(kept, already_in_context)``.

    A hit lands in ``already_in_context`` only when the hot-layer block
    for its ref exists AND fully contains the hit's rendered body.
    Order is preserved in both lists. Any failure reading the hot layer
    degrades to "keep everything" — dedup must never cost a result.
    """
    if not hits:
        return hits, []
    try:
        prefix = prefix_map(read_hot_layer(workspace))
    except Exception:  # noqa: BLE001 - degrade to no-dedup
        return hits, []
    if not prefix:
        return hits, []
    kept: list[SectionedHit] = []
    redundant: list[SectionedHit] = []
    for hit in hits:
        key = _hit_key(hit)
        block = prefix.get(key) if key else None
        rendered = (hit.summary or hit.body or hit.snippet or "").strip()
        if block and rendered and _norm(rendered) in block:
            redundant.append(hit)
        else:
            kept.append(hit)
    return kept, redundant


def render_in_context_section(hits: list[SectionedHit]) -> str:
    """Pointer lines for deduped hits, appended after the sectioned output.

    Phrased as a pointer ("drill if you need more"), NOT as a claim that
    the content is in context — the hot layer rotates between dreams, so
    a claim persisted into session history could go stale; a ref never does.
    """
    if not hits:
        return ""
    lines = [
        "## Matches shown in your Memory sections",
        "",
        "These results duplicate content already visible in the Memory "
        "sections of your system prompt — refs listed for citation. "
        "Use memory_drill on a uri only if you need the full body:",
    ]
    for hit in hits:
        ts = f" (ts {hit.ts})" if hit.ts else ""
        lines.append(f"- {hit.uri}{ts}")
    return "\n".join(lines)
