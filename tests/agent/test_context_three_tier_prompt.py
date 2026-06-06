"""3-tier system prompt for cache stability (Hermes-inspired Tier 2 C1).

Providers cache the input prompt by *prefix*. Mixing volatile content
(memory, recent history) with stable content (identity, bootstrap files)
breaks the cache anchor — the dynamic blocks shift on each turn, invalidating
the cached prefix even though most of the prompt was identical.

The 3-tier layout puts stable blocks first, session-stable blocks (agent
mode) in the middle, and volatile blocks last. Within one session the
stable + context prefix stays byte-identical across all turns where
memory / history / session summary change.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from durin.agent.context import ContextBuilder


def _make_builder(tmp_path):
    """Minimal ContextBuilder with memory/skills stubbed out via
    attribute assignment (ContextBuilder constructs MemoryStore /
    SkillsLoader internally; we override them after construction so we
    don't have to set up real workspace files)."""
    b = ContextBuilder(workspace=tmp_path)

    memory = MagicMock()
    memory.get_memory_context.return_value = ""
    memory.read_memory.return_value = ""
    memory.read_unprocessed_history.return_value = []
    memory.get_last_dream_cursor.return_value = None
    b.memory = memory

    skills = MagicMock()
    skills.get_always_skills.return_value = []
    skills.load_skills_for_context.return_value = ""
    skills.build_skills_summary.return_value = ""
    b.skills = skills
    return b


@pytest.fixture
def builder(tmp_path):
    return _make_builder(tmp_path)


def _set_memory(builder: ContextBuilder, text: str) -> None:
    builder.memory.get_memory_context.return_value = text
    builder.memory.read_memory.return_value = text


def _set_history(builder: ContextBuilder, entries: list[dict]) -> None:
    builder.memory.read_unprocessed_history.return_value = entries


def test_stable_layer_isolated_from_volatile(tmp_path):
    """A stable-only build must equal the start of a build that also has
    volatile content — verifying the volatile suffix is APPENDED, not
    interleaved."""
    b = _make_builder(tmp_path)
    only_stable = b.build_system_prompt(channel="cli")

    # Add volatile memory and rebuild.
    b.memory.get_memory_context.return_value = "User likes terse responses."
    b.memory.read_memory.return_value = "User likes terse responses."
    with_volatile = b.build_system_prompt(channel="cli")

    assert with_volatile.startswith(only_stable)


def test_volatile_blocks_appear_after_stable(builder):
    """The volatile signal (the session summary) must land AFTER the stable
    prefix — never before. §8e removed the legacy MEMORY.md + history.jsonl
    volatile blocks; that knowledge now lives in the stable pinned/hot tier, so
    the compaction summary is the remaining volatile signal."""
    prompt = builder.build_system_prompt(
        channel="cli", session_summary="Past discussion summarized."
    )

    # Identity (stable) — anchor on the "## Workspace" template heading.
    identity_pos = prompt.find("## Workspace")
    summary_pos = prompt.find("[Archived Context Summary]")

    assert identity_pos >= 0, "stable identity block must be present"
    assert summary_pos > identity_pos, "summary (volatile) must come after identity (stable)"


def test_context_layer_between_stable_and_volatile(builder, monkeypatch):
    """Agent mode suffix sits between stable prefix and volatile suffix —
    not at the top (which would interleave with stable) nor at the bottom
    (which would dilute its visibility as memory/history scroll past it).
    """
    _set_memory(builder, "Some memory.")
    # Mock the mode lookup so the suffix is deterministic.
    from durin.agent import context as ctx_mod
    fake_mode = MagicMock()
    fake_mode.prompt_suffix = "[ACTIVE MODE: PLAN]"

    def _fake_get_mode(_name):
        return fake_mode

    # The import is local to build_context_layer; patch via sys.modules.
    import durin.agent.agent_mode as agent_mode_mod
    monkeypatch.setattr(agent_mode_mod, "get_mode", _fake_get_mode)

    prompt = builder.build_system_prompt(
        channel="cli",
        agent_mode_name="PLAN",
        session_summary="Past summary.",
    )
    mode_pos = prompt.find("[ACTIVE MODE: PLAN]")
    memory_pos = prompt.find("[Archived Context Summary]")

    assert mode_pos > 0, "mode suffix must be present"
    assert mode_pos < memory_pos, "mode suffix must sit ABOVE the volatile summary"
    # Stable should still come first.
    identity_pos = prompt.find("## Workspace")
    assert identity_pos < mode_pos, "stable identity comes before mode suffix"


def test_volatile_changes_do_not_alter_stable_prefix(builder):
    """The CORE cache-stability invariant: two builds that differ ONLY in
    volatile content must share an identical prefix up to where the
    volatile layer begins."""
    # Build 1 — empty volatile.
    p1 = builder.build_system_prompt(channel="cli")

    # Build 2 — full volatile.
    _set_memory(builder, "Some non-trivial memory.")
    _set_history(builder, [{"timestamp": "2026-05-20", "content": "did X"}])
    p2 = builder.build_system_prompt(
        channel="cli", session_summary="A summary."
    )

    # The first build's content (which has no volatile) is exactly the
    # stable prefix of the second build.
    assert p2.startswith(p1), (
        "stable prefix must remain byte-identical when only volatile content changes"
    )


def test_empty_volatile_omits_separator(builder):
    """When the volatile layer is empty, no trailing ``---`` separator
    appears (cosmetic — keeps the prompt clean for cache inspection)."""
    p = builder.build_system_prompt(channel="cli")
    # No "---\n\n" at the very end.
    assert not p.rstrip().endswith("---")


def test_empty_context_layer_omits_separator(builder, monkeypatch):
    """When no agent_mode_name → context layer is empty → no extra
    separator between stable and volatile (or stable and end)."""
    # No agent_mode_name passed.
    p = builder.build_system_prompt(
        channel="cli", session_summary="A volatile summary."
    )
    # Sanity: stable + volatile present.
    assert "[Archived Context Summary]" in p
    assert "## Workspace" in p


def test_layer_skipping_when_all_layers_empty(tmp_path):
    """Edge case: an absolutely barebones builder with no content
    anywhere produces just the identity (stable only) — no leading or
    trailing separators."""
    b = _make_builder(tmp_path)
    p = b.build_system_prompt()
    assert p  # identity is non-empty
    assert not p.startswith("---")
    assert not p.endswith("---")
