"""Tests for persona-driven SOUL injection and operating floor in ContextBuilder."""

from durin.agent.context import ContextBuilder


def _write_default_soul(ws, body):
    (ws / "SOUL.md").write_text(body, encoding="utf-8")


def test_active_persona_soul_overrides_default(tmp_path):
    _write_default_soul(tmp_path, "# Soul\nI am the DEFAULT voice.")
    cb = ContextBuilder(tmp_path)
    prompt = cb.build_system_prompt(active_persona_soul="# Soul\nI am the RESEARCHER.")
    assert "I am the RESEARCHER." in prompt
    assert "I am the DEFAULT voice." not in prompt


def test_falls_back_to_default_soul_when_none(tmp_path):
    _write_default_soul(tmp_path, "# Soul\nI am the DEFAULT voice.")
    cb = ContextBuilder(tmp_path)
    prompt = cb.build_system_prompt(active_persona_soul=None)
    assert "I am the DEFAULT voice." in prompt


def test_operating_floor_present_for_pure_voice_soul(tmp_path):
    _write_default_soul(tmp_path, "x")
    cb = ContextBuilder(tmp_path)
    prompt = cb.build_system_prompt(active_persona_soul="# Soul\nPure voice, no rules.")
    assert "Act immediately on single-step tasks" in prompt  # the floor


def test_operating_floor_skipped_when_soul_has_execution_rules(tmp_path):
    _write_default_soul(tmp_path, "x")
    legacy = "# Soul\nVoice.\n\n## Execution Rules\n\n- Act immediately on single-step tasks — never end a turn with just a plan."
    cb = ContextBuilder(tmp_path)
    prompt = cb.build_system_prompt(active_persona_soul=legacy)
    # the rule text appears exactly once (from the soul, not duplicated by the floor)
    assert prompt.count("Act immediately on single-step tasks") == 1


def test_capture_directive_always_present(tmp_path):
    # A SOUL that embeds its own Execution Rules makes the operating floor empty —
    # the capture directive must STILL appear (now lives in identity.md).
    _write_default_soul(tmp_path, "x")
    legacy = "# Soul\nVoice.\n\n## Execution Rules\n\nDo the thing."
    cb = ContextBuilder(tmp_path)
    prompt = cb.build_system_prompt(active_persona_soul=legacy)
    assert "capture as you go" in prompt
    assert "memory_upsert_entity" in prompt
    assert "Correct in place" in prompt
