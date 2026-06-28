"""Tests for cache-friendly prompt construction."""

from __future__ import annotations

import datetime as datetime_module
from datetime import datetime as real_datetime
from importlib.resources import files as pkg_files
from pathlib import Path

from durin.agent.context import ContextBuilder


class _FakeDatetime(real_datetime):
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_bootstrap_files_are_backed_by_templates() -> None:
    template_dir = pkg_files("durin") / "templates"

    for filename in ContextBuilder.BOOTSTRAP_FILES:
        assert (template_dir / filename).is_file(), f"missing bootstrap template: {filename}"


def test_system_prompt_stays_stable_when_clock_changes(tmp_path, monkeypatch) -> None:
    """System prompt should not change just because wall clock minute changes."""
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    _FakeDatetime.current = real_datetime(2026, 2, 24, 13, 59)
    prompt1 = builder.build_system_prompt()

    _FakeDatetime.current = real_datetime(2026, 2, 24, 14, 0)
    prompt2 = builder.build_system_prompt()

    assert prompt1 == prompt2


def test_system_prompt_reflects_current_dream_memory_contract(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    # New memory model: entity pages + references, authored via memory_upsert_entity.
    assert "memory_upsert_entity" in prompt
    assert "references" in prompt.lower()
    # The dissolved legacy contract must be gone.
    assert "history.jsonl" not in prompt
    assert "automatically managed by Dream" not in prompt
    assert "memory_store" not in prompt


def test_runtime_context_is_separate_untrusted_user_message(tmp_path) -> None:
    """Runtime metadata should be merged with the user message."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]

    # Runtime context is now merged with user message into a single message
    assert messages[-1]["role"] == "user"
    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in user_content
    assert "Current Time:" in user_content
    assert "Channel: cli" in user_content
    assert "Chat ID: direct" in user_content
    assert "Return exactly: OK" in user_content


def test_runtime_context_appended_after_user_content(tmp_path) -> None:
    """User content must precede runtime context for prompt-cache prefix stability."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="hello world",
        channel="cli",
        chat_id="direct",
    )

    content = messages[-1]["content"]
    user_pos = content.find("hello world")
    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
    assert user_pos < tag_pos, "user content must precede runtime context for prefix stability"


def test_runtime_context_includes_sender_id_when_provided(tmp_path) -> None:
    """Sender ID should be included in runtime context when provided."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
        sender_id="user-12345",
    )

    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert "Sender ID: user-12345" in user_content


def test_runtime_context_excludes_sender_id_when_not_provided(tmp_path) -> None:
    """Sender ID should not be present in runtime context when not provided."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
        sender_id=None,
    )

    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert "Sender ID:" not in user_content


def test_execution_rules_in_system_prompt(tmp_path) -> None:
    """Execution rules should appear in the system prompt via default SOUL.md."""
    from durin.utils.helpers import sync_workspace_templates

    workspace = _make_workspace(tmp_path)
    sync_workspace_templates(workspace, silent=True)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()
    assert "single-step tasks" in prompt
    assert "multi-step tasks" in prompt
    assert "Read before you write" in prompt
    assert "verify the result" in prompt


def test_identity_has_no_behavioral_instructions(tmp_path) -> None:
    """Identity template should not contain behavioral rules or hardcoded name."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    identity = builder._get_identity(channel=None)
    assert "You are durin" not in identity
    assert "Act, don't narrate" not in identity
    assert "Execution Rules" not in identity


def test_system_prompt_does_not_warn_about_message_time_markers(tmp_path) -> None:
    """Parroting is prevented by not annotating assistant turns in history;
    no prompt-level warning about ``[Message Time: ...]`` is needed."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "Message Time" not in prompt


def test_operating_floor_template_contains_execution_rules() -> None:
    """Execution rules now live in the operating floor template, not SOUL.md.
    SOUL.md carries only voice/personality; the operating floor is always injected."""
    floor = (pkg_files("durin") / "templates" / "agent" / "operating_floor.md").read_text(encoding="utf-8")
    assert "## Operating Floor" in floor
    assert "single-step tasks" in floor
    assert "multi-step tasks" in floor
    soul = (pkg_files("durin") / "templates" / "SOUL.md").read_text(encoding="utf-8")
    assert "## Execution Rules" not in soul


def test_channel_format_hint_telegram(tmp_path) -> None:
    """Telegram channel should get messaging-app format hint."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(channel="telegram")
    assert "Format Hint" in prompt
    assert "messaging app" in prompt


def test_channel_format_hint_whatsapp(tmp_path) -> None:
    """WhatsApp should get plain-text format hint."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(channel="whatsapp")
    assert "Format Hint" in prompt
    assert "plain text only" in prompt


def test_channel_format_hint_absent_for_unknown(tmp_path) -> None:
    """Unknown or None channel should not inject a format hint."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(channel=None)
    assert "Format Hint" not in prompt

    prompt2 = builder.build_system_prompt(channel="feishu")
    assert "Format Hint" not in prompt2


def test_build_messages_passes_channel_to_system_prompt(tmp_path) -> None:
    """build_messages should pass channel through to build_system_prompt."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[], current_message="hi",
        channel="telegram", chat_id="123",
    )
    system = messages[0]["content"]
    assert "Format Hint" in system
    assert "messaging app" in system


def test_system_prompt_keeps_message_tool_out_of_current_chat_replies(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(channel="slack")

    assert "Do not use the 'message' tool for normal replies in the current chat" in prompt
    assert "Wait for the tool results, then answer once" in prompt


def test_subagent_result_does_not_create_consecutive_assistant_messages(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[{"role": "assistant", "content": "previous result"}],
        current_message="subagent result",
        channel="cli",
        chat_id="direct",
        current_role="assistant",
    )

    for left, right in zip(messages, messages[1:]):
        assert not (left.get("role") == right.get("role") == "assistant")


def test_always_skills_excluded_from_skills_index(tmp_path) -> None:
    """An always-on skill appears in Active Skills but NOT in the skills index."""
    workspace = _make_workspace(tmp_path)
    # Seed a synthetic always-on skill (the bundled memory skill was removed).
    probe = workspace / "skills" / "probe"
    probe.mkdir(parents=True, exist_ok=True)
    (probe / "SKILL.md").write_text(
        "---\nname: probe\ndescription: probe skill\nalways: true\nmetadata:\n  durin:\n    provenance:\n      source: test\n---\n\n# Probe\n",
        encoding="utf-8",
    )
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "# Active Skills" in prompt
    assert "### Skill: probe" in prompt

    skills_section = prompt.split("# Skills\n", 1)
    assert len(skills_section) > 1, "expected a '# Skills' index section in the prompt"
    index_text = skills_section[1].split("\n\n---")[0]
    assert "**probe**" not in index_text


