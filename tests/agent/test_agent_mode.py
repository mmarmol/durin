"""Tests for the agent-mode system — Sprint B / L3.

Covers:
- AgentMode dataclass + filter semantics
- Registry lookup
- Session helpers (enter_plan_mode / exit_plan_mode / set_mode)
- Runner-level filtering of tool definitions
- Context-builder prompt-suffix injection
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from durin.agent.agent_mode import (
    BUILD_MODE,
    DEFAULT_MODE,
    EXPLORE_MODE,
    PLAN_MODE,
    SESSION_MODE_KEY,
    SESSION_PRE_PLAN_KEY,
    AgentMode,
    enter_plan_mode,
    exit_plan_mode,
    filter_tools,
    get_active_mode,
    get_active_mode_name,
    get_mode,
    list_modes,
    set_mode,
)


def _session_stub(meta: dict | None = None):
    return SimpleNamespace(metadata=meta if meta is not None else {})


# ---------------------------------------------------------------------------
# AgentMode dataclass + filter semantics
# ---------------------------------------------------------------------------


class TestAgentMode:

    def test_build_mode_allows_everything(self):
        assert BUILD_MODE.is_tool_allowed("read_file")
        assert BUILD_MODE.is_tool_allowed("edit_file")
        assert BUILD_MODE.is_tool_allowed("anything_at_all")

    def test_plan_mode_allows_read_only_tools(self):
        assert PLAN_MODE.is_tool_allowed("read_file")
        assert PLAN_MODE.is_tool_allowed("grep")
        assert PLAN_MODE.is_tool_allowed("repo_overview")
        assert PLAN_MODE.is_tool_allowed("exit_plan_mode")

    def test_plan_mode_denies_mutation_tools(self):
        assert not PLAN_MODE.is_tool_allowed("edit_file")
        assert not PLAN_MODE.is_tool_allowed("write_file")
        assert not PLAN_MODE.is_tool_allowed("exec")

    def test_explore_mode_no_exit_affordance(self):
        # Explore mode (for subagents) does not include exit_plan_mode
        assert not EXPLORE_MODE.is_tool_allowed("exit_plan_mode")
        assert EXPLORE_MODE.is_tool_allowed("read_file")

    def test_denied_wins_over_allowed(self):
        custom = AgentMode(
            name="custom",
            description="test",
            allowed=frozenset({"read_file", "edit_file"}),
            denied=frozenset({"edit_file"}),
        )
        assert custom.is_tool_allowed("read_file")
        assert not custom.is_tool_allowed("edit_file")

    def test_allowed_none_means_all(self):
        custom = AgentMode(name="x", description="", allowed=None)
        assert custom.is_tool_allowed("anything")

    def test_prompt_suffix_present_for_plan(self):
        assert "PLAN MODE" in PLAN_MODE.prompt_suffix
        # build has no suffix
        assert BUILD_MODE.prompt_suffix == ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestModeRegistry:

    def test_list_modes_contains_defaults(self):
        names = {m.name for m in list_modes()}
        assert {"build", "plan", "explore"}.issubset(names)

    def test_get_mode_known(self):
        assert get_mode("plan").name == "plan"
        assert get_mode("build").name == "build"

    def test_get_mode_unknown_falls_back_to_build(self):
        assert get_mode("nonexistent").name == "build"

    def test_get_mode_none_falls_back_to_build(self):
        assert get_mode(None).name == "build"


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


class TestSessionHelpers:

    def test_default_mode_for_empty_metadata(self):
        session = _session_stub({})
        assert get_active_mode_name(session) == DEFAULT_MODE
        assert get_active_mode(session) is BUILD_MODE

    def test_default_mode_for_none_session(self):
        assert get_active_mode_name(None) == DEFAULT_MODE
        assert get_active_mode(None) is BUILD_MODE

    def test_enter_plan_mode_stashes_previous(self):
        session = _session_stub({SESSION_MODE_KEY: "build"})
        previous = enter_plan_mode(session)
        assert previous == "build"
        assert session.metadata[SESSION_MODE_KEY] == "plan"
        assert session.metadata[SESSION_PRE_PLAN_KEY] == "build"

    def test_enter_plan_mode_from_explore_preserves_prior(self):
        session = _session_stub({SESSION_MODE_KEY: "explore"})
        enter_plan_mode(session)
        assert session.metadata[SESSION_PRE_PLAN_KEY] == "explore"

    def test_enter_plan_mode_idempotent(self):
        session = _session_stub({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        prev = enter_plan_mode(session)
        assert prev == "plan"
        # pre_plan_mode preserved (was build)
        assert session.metadata[SESSION_PRE_PLAN_KEY] == "build"

    def test_exit_plan_mode_restores_previous(self):
        session = _session_stub({
            SESSION_MODE_KEY: "plan",
            SESSION_PRE_PLAN_KEY: "explore",
        })
        restored = exit_plan_mode(session)
        assert restored == "explore"
        assert session.metadata[SESSION_MODE_KEY] == "explore"
        assert SESSION_PRE_PLAN_KEY not in session.metadata

    def test_exit_plan_mode_no_pre_plan_defaults_to_build(self):
        session = _session_stub({SESSION_MODE_KEY: "plan"})
        restored = exit_plan_mode(session)
        assert restored == "build"

    def test_set_mode_explicit(self):
        session = _session_stub({SESSION_MODE_KEY: "build"})
        previous = set_mode(session, "explore")
        assert previous == "build"
        assert session.metadata[SESSION_MODE_KEY] == "explore"

    def test_set_mode_rejects_unknown(self):
        session = _session_stub({})
        with pytest.raises(ValueError):
            set_mode(session, "nonexistent_mode")


# ---------------------------------------------------------------------------
# filter_tools
# ---------------------------------------------------------------------------


class TestFilterTools:

    def test_fast_path_for_build(self):
        tools = [SimpleNamespace(name=n) for n in ("read_file", "edit_file", "exec")]
        assert filter_tools(tools, BUILD_MODE) is tools

    def test_filters_plan_mode(self):
        tools = [SimpleNamespace(name=n) for n in ("read_file", "edit_file", "grep", "exec")]
        out = filter_tools(tools, PLAN_MODE)
        names = {t.name for t in out}
        assert "read_file" in names
        assert "grep" in names
        assert "edit_file" not in names
        assert "exec" not in names


# ---------------------------------------------------------------------------
# Runner: _active_tool_definitions
# ---------------------------------------------------------------------------


class TestRunnerActiveToolDefinitions:

    def test_no_provider_returns_all(self):
        from durin.agent.runner import AgentRunSpec, AgentRunner
        from durin.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        # Inject schemas via cache so we don't need real Tool subclasses.
        registry._cached_definitions = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "edit_file"}},
        ]
        spec = AgentRunSpec(
            initial_messages=[],
            tools=registry,
            model="x",
            max_iterations=1,
            max_tool_result_chars=1000,
            mode_provider=None,
        )
        out = AgentRunner._active_tool_definitions(spec)
        assert {d["function"]["name"] for d in out} == {"read_file", "edit_file"}

    def test_plan_provider_filters(self):
        from durin.agent.runner import AgentRunSpec, AgentRunner
        from durin.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry._cached_definitions = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "edit_file"}},
            {"function": {"name": "exit_plan_mode"}},
        ]
        spec = AgentRunSpec(
            initial_messages=[],
            tools=registry,
            model="x",
            max_iterations=1,
            max_tool_result_chars=1000,
            mode_provider=lambda: PLAN_MODE,
        )
        out = AgentRunner._active_tool_definitions(spec)
        names = {d["function"]["name"] for d in out}
        assert names == {"read_file", "exit_plan_mode"}

    def test_provider_exception_falls_back_to_all(self):
        from durin.agent.runner import AgentRunSpec, AgentRunner
        from durin.agent.tools.registry import ToolRegistry

        def _boom():
            raise RuntimeError("oops")

        registry = ToolRegistry()
        registry._cached_definitions = [{"function": {"name": "read_file"}}]
        spec = AgentRunSpec(
            initial_messages=[],
            tools=registry,
            model="x",
            max_iterations=1,
            max_tool_result_chars=1000,
            mode_provider=_boom,
        )
        out = AgentRunner._active_tool_definitions(spec)
        # Doesn't crash; returns all defs as fast-path fallback.
        assert len(out) == 1


# ---------------------------------------------------------------------------
# ContextBuilder prompt suffix
# ---------------------------------------------------------------------------


class TestContextBuilderPromptSuffix:

    def test_plan_mode_suffix_injected(self, tmp_path):
        """build_system_prompt appends PLAN_MODE.prompt_suffix when active."""
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")

        prompt_with_plan = cb.build_system_prompt(agent_mode_name="plan")
        prompt_with_build = cb.build_system_prompt(agent_mode_name="build")

        assert "PLAN MODE" in prompt_with_plan
        # build's empty suffix means the suffix is absent
        assert "PLAN MODE" not in prompt_with_build

    def test_default_no_mode_no_suffix(self, tmp_path):
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")
        prompt = cb.build_system_prompt()
        assert "PLAN MODE" not in prompt

    def test_plan_suffix_appears_near_top_of_prompt(self, tmp_path):
        """The plan suffix is placed early so the model gives it weight.

        It comes immediately after the identity block, before bootstrap
        files / memory / skills catalog. We assert it lands within the
        first third of the prompt — looser than ``// 4`` because the
        identity block itself is non-trivially long (~2-3KB).
        """
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")
        prompt = cb.build_system_prompt(agent_mode_name="plan")
        plan_idx = prompt.find("PLAN MODE")
        assert plan_idx >= 0
        assert plan_idx < len(prompt) // 3, (
            f"PLAN MODE marker at {plan_idx} but prompt is {len(prompt)} chars; "
            "suffix should be near the top to maximize model attention"
        )


# ---------------------------------------------------------------------------
# Per-turn runtime reminder
# ---------------------------------------------------------------------------


class TestPlanModeRuntimeReminder:
    """When the session is in plan mode, every turn injects a reminder into
    the runtime context block. This is the OpenClaude pattern that prevents
    the model from "forgetting" the mode across long sessions."""

    def test_no_reminder_in_build_mode(self):
        from durin.agent.agent_mode import plan_mode_runtime_lines

        assert plan_mode_runtime_lines({SESSION_MODE_KEY: "build"}) == []

    def test_no_reminder_when_metadata_missing(self):
        from durin.agent.agent_mode import plan_mode_runtime_lines

        assert plan_mode_runtime_lines(None) == []
        assert plan_mode_runtime_lines({}) == []

    def test_reminder_present_in_plan_mode(self):
        from durin.agent.agent_mode import plan_mode_runtime_lines

        lines = plan_mode_runtime_lines({SESSION_MODE_KEY: "plan"})
        assert lines
        joined = "\n".join(lines)
        # Key phrases that must be present
        assert "PLAN MODE IS ACTIVE" in joined
        assert "supersedes" in joined
        assert "exit_plan_mode" in joined
        # Must address subagent escape-hatch
        assert "subagent" in joined.lower() or "delegate" in joined.lower()

    def test_reminder_appears_in_build_messages_when_plan_mode(self, tmp_path):
        """End-to-end: build_messages folds the reminder into the runtime
        context block of the user message."""
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")
        msgs = cb.build_messages(
            history=[],
            current_message="add a feature",
            channel="cli",
            chat_id="c",
            session_metadata={SESSION_MODE_KEY: "plan"},
        )
        last_content = msgs[-1]["content"]
        text = last_content if isinstance(last_content, str) else str(last_content)
        assert "PLAN MODE IS ACTIVE" in text
        assert "supersedes" in text

    def test_no_reminder_in_build_messages_when_build_mode(self, tmp_path):
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")
        msgs = cb.build_messages(
            history=[],
            current_message="add a feature",
            channel="cli",
            chat_id="c",
            session_metadata={SESSION_MODE_KEY: "build"},
        )
        last_content = msgs[-1]["content"]
        text = last_content if isinstance(last_content, str) else str(last_content)
        assert "PLAN MODE IS ACTIVE" not in text


# ---------------------------------------------------------------------------
# build_messages: approved_plan_path one-shot handoff
# ---------------------------------------------------------------------------


class TestApprovedPlanPathHandoff:
    """After /build approves a plan, the next turn must surface the plan path
    in the runtime-context block so the model knows where to read it from."""

    def test_approved_plan_path_appears_in_runtime_context(self, tmp_path):
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")
        meta = {"approved_plan_path": "/workspace/.durin/plans/plan_abc.md"}
        msgs = cb.build_messages(
            history=[],
            current_message="execute the plan",
            channel="cli",
            chat_id="c1",
            session_metadata=meta,
        )
        # The last user message embeds runtime context including the path.
        last = msgs[-1]
        text = last["content"] if isinstance(last["content"], str) else str(last["content"])
        assert "/workspace/.durin/plans/plan_abc.md" in text
        assert "Approved plan ready at" in text
        # The reminder includes the TodoWrite suggestion (Claude Code parity).
        assert "todo_write" in text

    def test_approved_plan_path_is_one_shot(self, tmp_path):
        """The handoff is consumed once — a second build_messages call with
        the same metadata dict does NOT re-inject the path."""
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")
        meta = {"approved_plan_path": "/workspace/plan.md"}
        cb.build_messages(
            history=[],
            current_message="first turn",
            channel="cli",
            chat_id="c1",
            session_metadata=meta,
        )
        # First call consumed the key
        assert "approved_plan_path" not in meta
        # Second call: nothing to inject
        msgs2 = cb.build_messages(
            history=[],
            current_message="second turn",
            channel="cli",
            chat_id="c1",
            session_metadata=meta,
        )
        last = msgs2[-1]
        text = last["content"] if isinstance(last["content"], str) else str(last["content"])
        assert "Approved plan ready at" not in text
