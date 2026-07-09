"""The stable system prompt advertises the rich-output block languages."""

from __future__ import annotations

from unittest.mock import MagicMock

from durin.agent.context import ContextBuilder


def _make_builder(tmp_path):
    """Minimal ContextBuilder with memory/skills stubbed out."""
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


def test_rich_output_section_present(tmp_path):
    b = _make_builder(tmp_path)
    prompt = b.build_system_prompt(channel="cli")
    assert "Rich output" in prompt
    assert "vega-lite" in prompt
    assert "mermaid" in prompt


def test_rich_output_steers_away_from_competing_paths(tmp_path):
    """The section must neutralize the two live-observed failure modes:
    delivering renderable content as a bare workspace file, and ASCII-art
    diagrams."""
    b = _make_builder(tmp_path)
    prompt = b.build_system_prompt(channel="cli")
    assert "Prefer the fence" in prompt
    assert "ASCII art" in prompt
