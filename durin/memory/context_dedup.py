"""Search-output dedup against the hot layer.

``memory_search`` hits whose rendered content is already visible in the
caller's system prompt — the hot layer's ``=== CANONICAL ===`` /
``=== FRAGMENT ===`` blocks — are pure token waste: the model pays for
the same text twice in the turn it lands, then re-reads it on every
subsequent turn of the session as part of replayed history.

The dedup is containment-based and false-negative-safe: a hit is
"already in context" when its rendered body (the exact text
``sectioned_output._render_block`` would print, ``summary > body >
snippet``) is a whitespace-normalised substring of the hot-layer block
for the SAME ref, OR when its ref is one the pinned block renders
*whole* — the always_on guidance pages, passed in by the caller as
``whole_refs``. The principal's page is deliberately not one of them:
the pinned block renders it with its body capped, so a hit on it can
carry text the prompt never showed. It goes through the containment
rule instead, which normally keeps it — the hot layer excludes the
pinned pages, so no block matches — at the cost of repeating at most
the capped excerpt. A hit that carries anything beyond what the prefix
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
    "dedup_key",
    "prefix_map",
    "prefix_map_from_text",
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


_RENDERED_BLOCK = re.compile(
    r"^=== (?P<kind>CANONICAL|FRAGMENT): (?P<header>.*?) ===\n"
    r"(?P<body>.*?)"
    r"^=== END (?P=kind) ===$",
    re.MULTILINE | re.DOTALL,
)


def prefix_map_from_text(text: str) -> dict[str, str]:
    """Same map as ``prefix_map``, recovered from an already-rendered hot layer.

    A caller whose prompt carries a *frozen* eager surface holds the hot layer
    as the text the model was shown, not as a ``HotLayer``: the blocks are
    parsed back out by their markers so containment is judged against exactly
    that text. Section prose between blocks is ignored, and an unterminated
    block (a truncated rendering) simply contributes no key.
    """
    out: dict[str, str] = {}
    for match in _RENDERED_BLOCK.finditer(text):
        key = match.group("header").rsplit(" (", 1)[0].strip()
        if not key:
            continue
        if match.group("kind") == "FRAGMENT" and key.endswith(".md"):
            key = key[: -len(".md")]
        out[key] = _norm(match.group("body"))
    return out


def dedup_key(ref: str) -> str:
    """Reduce a rendered marker's ref to the key `whole_refs` is matched on.

    The block markers print display uris — ``memory/entity_page/<type>:<slug>``
    for a canonical page, the entry path with its ``.md`` suffix for a
    fragment — while the dedup matches hits by the shape ``_hit_key`` below
    produces. A caller holding refs parsed out of rendered output (the
    per-turn prefetch) passes them through here first. Refs of other kinds
    are returned unchanged; they simply never match.
    """
    if ref.startswith("memory/entity_page/"):
        ref = ref[len("memory/entity_page/"):]
    if ref.endswith(".md"):
        ref = ref[: -len(".md")]
    return ref


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
    workspace: Path, hits: list[SectionedHit], *,
    pinned_refs: frozenset[str] = frozenset(),
    whole_refs: frozenset[str] | None = None,
    hot_layer_text: str | None = None,
) -> tuple[list[SectionedHit], list[SectionedHit]]:
    """Partition ``hits`` into ``(kept, already_in_context)``.

    The hot layer is read the same way the prompt builds it — with
    ``pinned_refs`` excluded from the canonical block — so the canonical
    slots this dedup sees are exactly the ones the model sees; a page
    freed up for the next-most-recent entity by that exclusion is a page
    this dedup must also see. A hit lands in ``already_in_context`` when
    its ref is in ``whole_refs`` (matched by plain membership, since the
    pinned block renders those pages whole and they never appear in the
    hot layer), or when the hot-layer block for its ref exists AND fully
    contains the hit's rendered body.

    ``hot_layer_text`` overrides that disk read with a hot layer already
    rendered — what a caller whose eager surface is frozen for the session
    actually has in context. Passing it (``""`` included: a session frozen
    over a workspace with nothing canonical carries a genuinely empty hot
    layer) means the workspace is never read, so a page written after the
    freeze cannot collapse a hit the model was never shown. The matching
    ``pinned_refs``/``whole_refs`` for that same frozen rendering are the
    caller's to pass.

    ``whole_refs`` is the subset of ``pinned_refs`` the pinned block
    renders whole — the always_on guidance — and defaults to
    ``pinned_refs`` when omitted. The principal's page belongs in
    ``pinned_refs`` (the prompt's hot layer excludes it) but NOT in
    ``whole_refs``: its body is capped, so its hits are judged by
    containment and normally pass, repeating at most the capped excerpt.

    Order is preserved in both lists. Any failure reading the hot layer
    degrades to "keep everything" — dedup must never cost a result.
    """
    if not hits:
        return hits, []
    whole = pinned_refs if whole_refs is None else whole_refs
    try:
        if hot_layer_text is None:
            prefix = prefix_map(read_hot_layer(workspace, exclude=pinned_refs))
        else:
            prefix = prefix_map_from_text(hot_layer_text)
    except Exception:  # noqa: BLE001 - degrade to no-dedup
        return hits, []
    if not prefix and not whole:
        return hits, []
    kept: list[SectionedHit] = []
    redundant: list[SectionedHit] = []
    for hit in hits:
        key = _hit_key(hit)
        if key and key in whole:
            redundant.append(hit)
            continue
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
