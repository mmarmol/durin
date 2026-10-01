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
    prefix — never before. The legacy MEMORY.md + history.jsonl volatile blocks
    were removed; that knowledge now lives in the stable pinned/hot tier, so
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


def test_a_bounded_decision_log_is_the_one_a_run_appends(builder):
    """When a turn's message only fits with its decision log cut, the
    builder records the bound, and the task state rendered with it is the
    one the message carries: a run appending the task state mid-turn
    compares against exactly that, and appends nothing while it holds."""
    from durin.agent.task_state import task_state_runtime_lines
    from durin.session.decision_log import DECISION_LOG_KEY
    from durin.utils.helpers import estimate_prompt_tokens

    metadata = {DECISION_LOG_KEY: [
        {"text": f"decision {i}: " + "the second approach is simpler " * 6, "ts": "", "source": "auto"}
        for i in range(10)
    ]}
    whole = builder.build_messages(history=[], current_message="hello", session_metadata=metadata)
    assert builder.last_decision_log_tokens is None
    budget = estimate_prompt_tokens(whole) - 200

    messages = builder.build_messages(
        history=[], current_message="hello", session_metadata=metadata, input_budget_tokens=budget,
    )

    bound = builder.last_decision_log_tokens
    assert bound is not None
    assert estimate_prompt_tokens(messages) <= budget
    block = "\n".join(task_state_runtime_lines(metadata, decision_log_max_tokens=bound))
    assert "decision 9:" in block and "decision 0:" not in block
    assert block in messages[-1]["content"]


def _long_summary() -> str:
    blocks = [f"- Span {i}: we worked on item {i}. " + "detail " * 60 for i in range(40)]
    return (
        "=== ARCHIVED SUMMARY (consolidator) ===\n" + "\n\n---\n".join(blocks)[:15_900]
        + "\n=== END ARCHIVED SUMMARY ===\nThe turns summarized above are archived."
    )


def _fixed_tokens(builder) -> int:
    from durin.utils.helpers import estimate_text_tokens

    return estimate_text_tokens(builder.build_system_prompt())


def test_the_summary_a_prompt_carries_does_not_change_with_the_message(builder):
    """The summary rides in the system prompt, so a cut that changes from one
    turn to the next misses every provider's prompt cache for the whole
    prompt. The cut followed each message's length, and the system prompt
    changed on nearly every turn of a small window."""
    summary = _long_summary()
    budget = _fixed_tokens(builder) + 3_500

    short = builder.build_messages(
        history=[], current_message="ok", session_summary=summary, input_budget_tokens=budget,
    )
    longer = builder.build_messages(
        history=[], current_message="please look at this: " + "note " * 400,
        session_summary=summary, input_budget_tokens=budget,
    )

    assert "older parts of this summary are left out" in short[0]["content"]
    assert longer[0]["content"] == short[0]["content"]


def test_a_summary_that_fits_whole_is_carried_whole(builder):
    """Where the whole summary, the message and the history fit the budget,
    the prompt carries the summary as it was: the cut is for windows too small
    for it, not for the windows that always carried it whole."""
    from durin.utils.helpers import estimate_text_tokens

    summary = _long_summary()
    budget = _fixed_tokens(builder) + estimate_text_tokens(summary) + 3_000

    messages = builder.build_messages(
        history=[], current_message="ok", session_summary=summary, input_budget_tokens=budget,
    )

    assert summary in messages[0]["content"]
