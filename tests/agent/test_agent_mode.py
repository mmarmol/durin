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
    READ_MODE,
    SESSION_MODE_KEY,
    SESSION_PRE_PLAN_KEY,
    AgentMode,
    clear_executing_plan_if_todos_done,
    enter_plan_mode,
    executing_plan_runtime_lines,
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

    def test_read_mode_is_read_only_and_neutral(self):
        # `read` shares explore's read-only surface...
        assert READ_MODE.is_tool_allowed("read_file")
        assert READ_MODE.is_tool_allowed("grep")
        assert READ_MODE.is_tool_allowed("web_search")
        assert not READ_MODE.is_tool_allowed("edit_file")
        assert not READ_MODE.is_tool_allowed("exec")
        assert not READ_MODE.is_tool_allowed("exit_plan_mode")
        assert READ_MODE.builtin
        # ...but its posture is NEUTRAL: none of the interactive/sub-agent framing that
        # derails a workflow node (no "bail", no "exit_plan_mode", no "parent agent").
        suffix = READ_MODE.prompt_suffix.lower()
        for forbidden in ("exit_plan_mode", "sub-agent", "parent agent", "cannot complete"):
            assert forbidden not in suffix

    def test_read_mode_is_registered_builtin(self):
        assert get_mode("read") is READ_MODE
        assert READ_MODE in list_modes()

    def test_curation_plan_gains_memory_and_capability_discovery(self):
        # Planning must recall project memory and see available capabilities.
        for tool in ("memory_search", "memory_drill", "skills_list",
                     "skill_view", "skill_search", "list_workflows"):
            assert PLAN_MODE.is_tool_allowed(tool), tool
        # But not setup/sensitive/mutation tools.
        for tool in ("mcp_search", "list_secrets", "request_secret", "run_workflow"):
            assert not PLAN_MODE.is_tool_allowed(tool), tool

    def test_curation_explore_read_gain_memory_recall_and_stay_coupled(self):
        # A delegated read-only investigator can recall project memory...
        for mode in (EXPLORE_MODE, READ_MODE):
            assert mode.is_tool_allowed("memory_search")
            assert mode.is_tool_allowed("memory_drill")
        # ...but stays lean: no skill/workflow discovery, no secrets, and
        # session_search is useless in a freshly-spawned subagent session.
        for tool in ("skills_list", "skill_search", "list_workflows",
                     "session_search", "list_secrets"):
            assert not EXPLORE_MODE.is_tool_allowed(tool), tool
        # explore and read intentionally share ONE read-only tool surface;
        # only their prompt posture differs.
        assert EXPLORE_MODE.allowed == READ_MODE.allowed

    def test_memory_recall_tools_are_subagent_scoped(self):
        # A mode allowlist can only filter tools the scope already loaded, so
        # the read-only modes' memory recall must carry the "subagent" scope —
        # otherwise it would never surface in a subagent / workflow-node run.
        from durin.agent.tools.memory_drill import MemoryDrillTool
        from durin.agent.tools.memory_search import MemorySearchTool
        assert "subagent" in MemorySearchTool._scopes
        assert "subagent" in MemoryDrillTool._scopes

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
        from durin.agent.runner import AgentRunner, AgentRunSpec
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
        from durin.agent.runner import AgentRunner, AgentRunSpec
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
        from durin.agent.runner import AgentRunner, AgentRunSpec
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

    def test_plan_suffix_precedes_volatile_blocks(self, tmp_path):
        """The plan suffix sits in the **context** tier of the 3-tier
        prompt (Tier 2 C1) — after the stable prefix (identity, bootstrap,
        skills catalog) but before any volatile block (archived session
        summary, memory, recent history). This preserves model attention
        to the suffix relative to volatile blocks while keeping the
        stable prefix byte-identical across turns for prompt-cache hits.

        Uses ``session_summary`` as the volatile probe because its marker
        is unambiguous; the ``# Memory`` header also appears as content
        inside the bundled "memory" skill (in the stable Active Skills
        block), so a plain ``"# Memory"`` search would hit a false positive.
        """
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")
        prompt = cb.build_system_prompt(
            agent_mode_name="plan",
            session_summary="Prior conversation context summarized.",
        )
        plan_idx = prompt.find("PLAN MODE")
        summary_idx = prompt.find("[Archived Context Summary]")
        assert plan_idx >= 0
        assert summary_idx >= 0
        assert plan_idx < summary_idx, (
            f"PLAN MODE at {plan_idx} must precede session summary at {summary_idx}"
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


class TestExecutingPlanRuntimeLines:
    """`executing_plan_path` is the persistent counterpart to the one-shot
    `approved_plan_path`: it is re-injected every turn so the "you are
    executing an approved plan" frame survives compaction (the path lives in
    session.metadata, which survives, but the model only sees it if rendered
    back each turn). Mirrors how `todos_runtime_lines` keeps the todo list
    alive across compaction.
    """

    def test_emits_pointer_when_set(self):
        lines = executing_plan_runtime_lines(
            {"executing_plan_path": "/ws/.durin/plans/plan_x.md"}
        )
        text = "\n".join(lines)
        assert "/ws/.durin/plans/plan_x.md" in text
        # It points at the plan + defers progress to the todo list (so the
        # agent does not re-run completed steps — progress is the cursor).
        assert "todo_write" in text

    def test_empty_when_absent_or_invalid(self):
        assert executing_plan_runtime_lines({}) == []
        assert executing_plan_runtime_lines(None) == []
        assert executing_plan_runtime_lines({"executing_plan_path": ""}) == []
        assert executing_plan_runtime_lines({"executing_plan_path": "  "}) == []

    def test_not_one_shot_does_not_mutate_metadata(self):
        meta = {"executing_plan_path": "/ws/plan.md"}
        executing_plan_runtime_lines(meta)
        executing_plan_runtime_lines(meta)
        # Unlike approved_plan_path, the key is NOT consumed.
        assert meta["executing_plan_path"] == "/ws/plan.md"

    def test_survives_compaction_persistently_in_build_messages(self, tmp_path):
        """Two successive build_messages calls with the same executing plan
        both inject the pointer — i.e. it survives an intervening compaction,
        unlike the one-shot approved_plan_path."""
        from durin.agent.context import ContextBuilder

        cb = ContextBuilder(workspace=tmp_path, timezone="UTC")
        meta = {"executing_plan_path": "/ws/.durin/plans/plan_y.md"}
        for turn in ("turn before compaction", "turn after compaction"):
            msgs = cb.build_messages(
                history=[],
                current_message=turn,
                channel="cli",
                chat_id="c1",
                session_metadata=meta,
            )
            last = msgs[-1]
            text = (
                last["content"]
                if isinstance(last["content"], str)
                else str(last["content"])
            )
            assert "/ws/.durin/plans/plan_y.md" in text
        # Key was never consumed.
        assert meta["executing_plan_path"] == "/ws/.durin/plans/plan_y.md"


class TestClearExecutingPlanWhenDone:
    """The plan pointer must stop re-injecting once the plan's todos (the
    execution cursor) are all completed — otherwise it lingers into later,
    unrelated turns."""

    @staticmethod
    def _todo(content, status):
        return {"content": content, "status": status, "activeForm": content}

    def test_clears_when_all_todos_completed(self):
        meta = {
            "executing_plan_path": "/ws/plan.md",
            "todos": [self._todo("a", "completed"), self._todo("b", "completed")],
        }
        assert clear_executing_plan_if_todos_done(meta) is True
        assert "executing_plan_path" not in meta

    def test_keeps_when_a_todo_is_pending_or_in_progress(self):
        meta = {
            "executing_plan_path": "/ws/plan.md",
            "todos": [self._todo("a", "completed"), self._todo("b", "in_progress")],
        }
        assert clear_executing_plan_if_todos_done(meta) is False
        assert meta["executing_plan_path"] == "/ws/plan.md"

    def test_keeps_when_no_todos(self):
        meta = {"executing_plan_path": "/ws/plan.md"}
        assert clear_executing_plan_if_todos_done(meta) is False
        assert meta["executing_plan_path"] == "/ws/plan.md"

    def test_noop_when_no_executing_plan(self):
        meta = {"todos": [self._todo("a", "completed")]}
        assert clear_executing_plan_if_todos_done(meta) is False
        assert clear_executing_plan_if_todos_done(None) is False


class TestUpdatePlanStall:
    """Stall stop-condition: surfacing only — the counter trips a runtime
    reminder, it never blocks anything (the V7/V8 PlanHook stays refuted)."""

    def _meta(self, todos=None):
        from durin.agent.agent_mode import EXECUTING_PLAN_PATH_KEY
        from durin.session.todo_state import TODOS_KEY

        meta = {EXECUTING_PLAN_PATH_KEY: "/tmp/plan.md"}
        if todos is not None:
            meta[TODOS_KEY] = todos
        return meta

    def _todo(self, content, status="pending"):
        return {"content": content, "status": status, "activeForm": content}

    def test_unchanged_todos_increment_and_trip_notice_at_threshold(self):
        from durin.agent.agent_mode import (
            PLAN_STALL_NOTICE_KEY,
            update_plan_stall,
        )

        meta = self._meta(todos=[self._todo("a")])
        assert update_plan_stall(meta, threshold=2) is False  # turn 0: baseline
        assert update_plan_stall(meta, threshold=2) is False  # turn 1: count=1
        assert update_plan_stall(meta, threshold=2) is True   # turn 2: count=2
        assert meta[PLAN_STALL_NOTICE_KEY] == 2

    def test_todo_change_resets_counter_and_clears_notice(self):
        from durin.agent.agent_mode import (
            PLAN_STALL_NOTICE_KEY,
            update_plan_stall,
        )
        from durin.session.todo_state import TODOS_KEY

        meta = self._meta(todos=[self._todo("a")])
        for _ in range(3):
            update_plan_stall(meta, threshold=2)
        assert PLAN_STALL_NOTICE_KEY in meta
        meta[TODOS_KEY] = [self._todo("a", status="completed")]
        assert update_plan_stall(meta, threshold=2) is False
        assert PLAN_STALL_NOTICE_KEY not in meta

    def test_no_executing_plan_clears_all_stall_keys(self):
        from durin.agent.agent_mode import (
            EXECUTING_PLAN_PATH_KEY,
            PLAN_STALL_COUNT_KEY,
            PLAN_STALL_FINGERPRINT_KEY,
            PLAN_STALL_NOTICE_KEY,
            update_plan_stall,
        )

        meta = self._meta(todos=[self._todo("a")])
        for _ in range(3):
            update_plan_stall(meta, threshold=2)
        meta.pop(EXECUTING_PLAN_PATH_KEY)
        assert update_plan_stall(meta, threshold=2) is False
        for key in (
            PLAN_STALL_COUNT_KEY,
            PLAN_STALL_FINGERPRINT_KEY,
            PLAN_STALL_NOTICE_KEY,
        ):
            assert key not in meta

    def test_threshold_zero_disables(self):
        from durin.agent.agent_mode import PLAN_STALL_COUNT_KEY, update_plan_stall

        meta = self._meta(todos=[self._todo("a")])
        for _ in range(5):
            assert update_plan_stall(meta, threshold=0) is False
        assert PLAN_STALL_COUNT_KEY not in meta

    def test_runtime_lines_include_reassess_when_notice_set(self):
        from durin.agent.agent_mode import (
            PLAN_STALL_NOTICE_KEY,
            executing_plan_runtime_lines,
        )

        meta = self._meta()
        meta[PLAN_STALL_NOTICE_KEY] = 8
        lines = executing_plan_runtime_lines(meta)
        joined = "\n".join(lines)
        assert "Executing approved plan" in joined
        assert "8 consecutive turns" in joined
        assert "Reassess" in joined

    def test_runtime_lines_no_reassess_without_notice(self):
        from durin.agent.agent_mode import executing_plan_runtime_lines

        lines = executing_plan_runtime_lines(self._meta())
        assert "Reassess" not in "\n".join(lines)


def test_plan_stall_threshold_resolution():
    from types import SimpleNamespace

    from durin.agent.agent_mode import plan_stall_threshold

    cfg = SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(plan_stall_turns=3)))
    assert plan_stall_threshold(cfg) == 3
    assert plan_stall_threshold(None) == 8  # default when no app_config (tests)


def test_plan_stall_turns_config_default():
    from durin.config.schema import AgentDefaults

    d = AgentDefaults()
    assert d.plan_stall_turns == 8

    disabled = AgentDefaults(plan_stall_turns=0)
    assert disabled.plan_stall_turns == 0


class TestVerificationGuidance:
    """The model is told upfront that plans need verification criteria —
    guidance that reduces lint-reject round-trips (the enforcement itself
    is the exit_plan_mode lint)."""

    def test_plan_mode_suffix_mentions_verification_requirement(self):
        from durin.agent.agent_mode import PLAN_MODE

        assert "## Verification" in PLAN_MODE.prompt_suffix
        assert "verify:" in PLAN_MODE.prompt_suffix

    def test_exit_plan_mode_schema_mentions_verification(self):
        from durin.agent.tools.plan_mode import ExitPlanModeTool

        tool = ExitPlanModeTool(sessions=None)
        desc = tool.parameters["properties"]["plan"]["description"]
        assert "## Verification" in desc
        assert "rejected" in desc
