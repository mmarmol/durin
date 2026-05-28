"""Assembles the v2 Dream consolidator prompt from the package files.

Per `docs/memory/06_prompts_and_instructions.md` §4 the prompt is a
concatenation of:

  1. ``durin/templates/dream/consolidator.md`` (template; carries
     ``{slot}`` placeholders this builder substitutes).
  2. ``rules.md``
  3. ``commit_format.md``
  4. ``json_patch_reference.md``
  5. each file in ``examples/`` sorted lexicographically.

Why concatenation instead of role-based messages: the runner passes a
single prompt to ``litellm.completion`` today; multi-message scaffolds
add complexity for marginal gain at this scale, and tests against
GLM-5.1 in Phase 0.3 confirmed concatenation works.

The builder is **read-only against disk** — the package files are
treated as source-of-truth artifacts. Edits go to the .md files; the
builder picks them up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = [
    "DreamPromptContext",
    "build_dream_prompt",
]


_TEMPLATE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "templates"
    / "dream"
)

# Spec §5.1: cap the existing-URIs list so the prompt stays bounded
# even in mature workspaces with thousands of entities. The 100 figure
# comes from `docs/memory/06_prompts_and_instructions.md` §4.2.
_EXISTING_URIS_CAP: int = 100


@dataclass(frozen=True)
class DreamPromptContext:
    """All inputs the consolidator prompt needs filled in.

    The runner populates this from disk + git + the pending entries
    discovered for the entity. Empty / missing values are fine —
    :func:`build_dream_prompt` renders them visibly as ``(none)`` so
    the LLM doesn't read silence as missing context.
    """

    entity_id: str
    existing_page_content: str
    existing_attribute_keys: tuple[str, ...] = field(default_factory=tuple)
    existing_relation_types: tuple[str, ...] = field(default_factory=tuple)
    existing_uris: tuple[str, ...] = field(default_factory=tuple)
    recent_history: str = ""
    entries: tuple[str, ...] = field(default_factory=tuple)
    # B-19 (2026-05-29): surface the current relation count so the LLM
    # can budget against the 200 hard cap before fanning out new
    # `/relations/-` ops. See Rule 9 in `rules.md`.
    current_relation_count: int = 0


def build_dream_prompt(ctx: DreamPromptContext) -> str:
    """Return the full prompt string ready to send to the LLM."""
    template = _read(_TEMPLATE_ROOT / "consolidator.md")
    filled = _fill_slots(template, ctx)
    package = "\n\n".join(
        [
            filled,
            "---\n\n## Rules\n\n" + _read(_TEMPLATE_ROOT / "rules.md"),
            "---\n\n## Output format\n\n"
            + _read(_TEMPLATE_ROOT / "commit_format.md"),
            "---\n\n## JSON Patch reference\n\n"
            + _read(_TEMPLATE_ROOT / "json_patch_reference.md"),
            "---\n\n## Examples\n\n" + _read_examples(),
        ]
    )
    return package


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip("\n")


def _read_examples() -> str:
    """Concatenate all `examples/*.md` files in lexicographic order."""
    examples_dir = _TEMPLATE_ROOT / "examples"
    parts: list[str] = []
    for path in sorted(examples_dir.glob("*.md")):
        parts.append(_read(path))
    return "\n\n---\n\n".join(parts)


def _fill_slots(template: str, ctx: DreamPromptContext) -> str:
    """Substitute the ``{slot}`` placeholders in `consolidator.md`."""
    attrs = _render_list_inline(ctx.existing_attribute_keys)
    rels = _render_list_inline(ctx.existing_relation_types)
    uris = _render_bulleted(ctx.existing_uris, cap=_EXISTING_URIS_CAP)
    history = ctx.recent_history.strip() or "(no recent history)"
    entries_text = _render_bulleted(ctx.entries, cap=None)
    n_entries = len(ctx.entries)
    page = ctx.existing_page_content or "(no existing page on disk)"

    return (
        template
        .replace("{entity_id}", ctx.entity_id)
        .replace("{existing_page_content}", page)
        .replace("{existing_attribute_keys}", attrs)
        .replace("{existing_relation_types}", rels)
        .replace("{existing_uris}", uris)
        .replace("{recent_history}", history)
        .replace("{n_entries}", str(n_entries))
        .replace("{entries_text}", entries_text)
        .replace("{current_relation_count}", str(max(0, int(ctx.current_relation_count))))
    )


def _render_list_inline(items: Iterable[str]) -> str:
    """Render a list of strings as a comma-separated inline value."""
    cleaned = [s for s in items if isinstance(s, str) and s.strip()]
    if not cleaned:
        return "(none)"
    return ", ".join(sorted(cleaned))


def _render_bulleted(items: Iterable[str], *, cap: int | None) -> str:
    """Render as `- item` lines; ``cap`` truncates with an ellipsis line."""
    cleaned = [s for s in items if isinstance(s, str) and s.strip()]
    if not cleaned:
        return "(none)"
    original_len = len(cleaned)
    if cap is not None and original_len > cap:
        cleaned = cleaned[:cap]
        ellipsis = f"  (+{original_len - cap} more not shown)"
    else:
        ellipsis = ""
    lines = [f"- {s}" for s in cleaned]
    if ellipsis:
        lines.append(ellipsis)
    return "\n".join(lines)
