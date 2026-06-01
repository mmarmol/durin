"""Prompt builder for the v2 Dream consolidator.

Per `docs/architecture/memory/06_prompts_and_instructions.md` §4 the builder
concatenates:

  1. `consolidator.md` (template with {slot} placeholders)
  2. `rules.md`
  3. `commit_format.md`
  4. `json_patch_reference.md`
  5. each file in `examples/` sorted lexicographically

Slots filled in `consolidator.md`:
  - {entity_id}
  - {existing_page_content}
  - {existing_attribute_keys}  (sorted, comma-separated, or "(none)")
  - {existing_relation_types}  (sorted, comma-separated, or "(none)")
  - {existing_uris}            (newline-bulleted, truncated)
  - {recent_history}           (multi-line git log block)
  - {n_entries}
  - {entries_text}             (newline-bulleted observations)
  - {current_relation_count}   (integer; see Rule 9, B-19 cap surface)
"""

from __future__ import annotations

import pytest

from durin.memory.dream_prompt_builder import (
    DreamPromptContext,
    build_dream_prompt,
)


@pytest.fixture()
def base_ctx() -> DreamPromptContext:
    return DreamPromptContext(
        entity_id="person:marcelo",
        existing_page_content="---\ntype: person\nname: Marcelo\n---\n",
        existing_attribute_keys=("email", "current_residence"),
        existing_relation_types=("spouse",),
        existing_uris=("person:marcelo", "person:susana", "project:durin"),
        recent_history="- 2026-05-25 (Dream): add spouse relation",
        entries=(
            "episodic/2026-05-26T08-45.md: Marcelo's new email is "
            "mmarmol@mxhero.com",
        ),
    )


# ---------------------------------------------------------------------------
# Slot substitution
# ---------------------------------------------------------------------------


def test_entity_id_appears(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "ENTITY: person:marcelo" in out


def test_existing_page_inlined(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "name: Marcelo" in out


def test_existing_attribute_keys_listed(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "email" in out
    assert "current_residence" in out


def test_existing_relation_types_listed(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "spouse" in out


def test_empty_schema_says_none(base_ctx: DreamPromptContext) -> None:
    ctx = DreamPromptContext(
        entity_id="person:new",
        existing_page_content="",
        existing_attribute_keys=(),
        existing_relation_types=(),
        existing_uris=(),
        recent_history="",
        entries=("episodic/x.md: ...",),
    )
    out = build_dream_prompt(ctx)
    # Empty lists render visibly so the LLM doesn't read them as missing.
    assert "(none)" in out


def test_existing_uris_truncated_at_100(base_ctx: DreamPromptContext) -> None:
    many = tuple(f"topic:t{i:03d}" for i in range(250))
    ctx = DreamPromptContext(
        entity_id="topic:focus",
        existing_page_content="",
        existing_attribute_keys=(),
        existing_relation_types=(),
        existing_uris=many,
        recent_history="",
        entries=("episodic/x.md: ...",),
    )
    out = build_dream_prompt(ctx)
    # Spec: truncate to 100 entries; the count of unique URIs in the
    # rendered "EXISTING ENTITY URIs" block must be ≤ 100.
    block = out.split("EXISTING ENTITY URIs", 1)[1].split(
        "SUGGESTED STARTER TYPES", 1,
    )[0]
    count = sum(1 for line in block.splitlines() if line.strip().startswith("- "))
    assert count <= 100


def test_n_entries_replaced(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "(1)" in out or "PENDING OBSERVATIONS (1):" in out


def test_entries_text_inlined(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "mmarmol@mxhero.com" in out


def test_recent_history_inlined(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "add spouse relation" in out


# ---------------------------------------------------------------------------
# Package assembly
# ---------------------------------------------------------------------------


def test_rules_section_appended(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    # Spec sentinels from rules.md.
    assert "Rule 1" in out
    assert "Provenance is non-negotiable" in out


def test_json_patch_reference_appended(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "JSON Patch operations reference" in out


def test_commit_format_appended(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert "Commit message format" in out
    assert "Cursor-after:" in out


def test_examples_appended(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    # All 6 example file titles must surface.
    assert "Example 01" in out
    assert "Example 02" in out
    assert "Example 03" in out
    assert "Example 04" in out
    assert "Example 05" in out
    assert "Example 06" in out


def test_examples_in_lexicographic_order(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert out.index("Example 01") < out.index("Example 02")
    assert out.index("Example 02") < out.index("Example 03")
    assert out.index("Example 05") < out.index("Example 06")


# ---------------------------------------------------------------------------
# Output sanity
# ---------------------------------------------------------------------------


def test_no_unsubstituted_placeholders(base_ctx: DreamPromptContext) -> None:
    """If a placeholder leaks through, the LLM sees literal `{slot}`
    text and gets confused."""
    out = build_dream_prompt(base_ctx)
    import re
    leaked = re.findall(r"\{[a-z_]+\}", out)
    assert leaked == [], f"unsubstituted placeholders: {leaked}"


def test_prompt_is_nonempty(base_ctx: DreamPromptContext) -> None:
    out = build_dream_prompt(base_ctx)
    assert len(out) > 500


# ---------------------------------------------------------------------------
# B-19 (2026-05-29): current_relation_count surfaces the cap budget
# ---------------------------------------------------------------------------


def test_current_relation_count_rendered() -> None:
    """The integer count is interpolated into the prompt so the LLM
    can budget against the 200 hard cap (Rule 9) before fanning out."""
    ctx = DreamPromptContext(
        entity_id="person:marcelo",
        existing_page_content="",
        existing_attribute_keys=(),
        existing_relation_types=(),
        existing_uris=(),
        recent_history="",
        entries=("episodic/x.md: ...",),
        current_relation_count=187,
    )
    out = build_dream_prompt(ctx)
    assert "current relation count: 187" in out


def test_current_relation_count_defaults_to_zero() -> None:
    """A fresh entity (no page yet) renders as `0`, not as missing."""
    ctx = DreamPromptContext(
        entity_id="person:new",
        existing_page_content="",
        existing_attribute_keys=(),
        existing_relation_types=(),
        existing_uris=(),
        recent_history="",
        entries=("episodic/x.md: ...",),
    )
    out = build_dream_prompt(ctx)
    assert "current relation count: 0" in out


def test_rule_9_appended() -> None:
    """Rule 9 is part of the rules.md package the LLM reads."""
    ctx = DreamPromptContext(
        entity_id="person:marcelo",
        existing_page_content="",
        existing_attribute_keys=(),
        existing_relation_types=(),
        existing_uris=(),
        recent_history="",
        entries=("episodic/x.md: ...",),
    )
    out = build_dream_prompt(ctx)
    assert "Rule 9" in out
    assert "200" in out
    assert "Per-entity relation cap" in out
