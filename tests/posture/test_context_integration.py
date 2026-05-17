"""Tests for posture phrase injection into the system prompt."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from durin.agent.context import ContextBuilder


class TestPosturePhraseInjection:
    def test_posture_phrase_included_in_system_prompt(self, tmp_path: Path):
        ctx = ContextBuilder(tmp_path)
        prompt = ctx.build_system_prompt(posture_phrase="Priorizá reversibilidad. No rompas lo que funciona.")
        assert "# Posture" in prompt
        assert "Priorizá reversibilidad" in prompt

    def test_no_posture_section_when_phrase_is_none(self, tmp_path: Path):
        ctx = ContextBuilder(tmp_path)
        prompt = ctx.build_system_prompt(posture_phrase=None)
        assert "# Posture" not in prompt

    def test_no_posture_section_when_phrase_is_empty(self, tmp_path: Path):
        ctx = ContextBuilder(tmp_path)
        prompt = ctx.build_system_prompt(posture_phrase="")
        assert "# Posture" not in prompt

    def test_posture_appears_after_memory_before_skills(self, tmp_path: Path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("- some memory entry")

        ctx = ContextBuilder(tmp_path)
        prompt = ctx.build_system_prompt(posture_phrase="Ejecutá lo pedido sin desvíos.")

        postura_idx = prompt.find("# Posture")
        assert postura_idx > 0

    def test_build_messages_passes_posture_phrase(self, tmp_path: Path):
        ctx = ContextBuilder(tmp_path)
        messages = ctx.build_messages(
            history=[],
            current_message="hello",
            posture_phrase="Sé directo, primera opción razonable.",
        )
        system_content = messages[0]["content"]
        assert "# Posture" in system_content
        assert "Sé directo" in system_content

    def test_build_messages_no_posture_when_none(self, tmp_path: Path):
        ctx = ContextBuilder(tmp_path)
        messages = ctx.build_messages(
            history=[],
            current_message="hello",
            posture_phrase=None,
        )
        system_content = messages[0]["content"]
        assert "# Posture" not in system_content
