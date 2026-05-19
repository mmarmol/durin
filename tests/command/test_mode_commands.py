"""Tests for /plan, /build, /mode slash commands (Sprint B / L3)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from durin.agent.agent_mode import (
    BUILD_MODE,
    PLAN_MODE,
    SESSION_MODE_KEY,
    SESSION_PRE_PLAN_KEY,
)
from durin.bus.events import InboundMessage
from durin.command.builtin import (
    BUILTIN_COMMAND_SPECS,
    builtin_command_palette,
    cmd_build,
    cmd_mode,
    cmd_plan,
)
from durin.command.router import CommandContext, CommandRouter
from durin.command.builtin import register_builtin_commands
from durin.telemetry.logger import (
    TelemetryLogger,
    bind_telemetry,
    reset_telemetry,
)


def _make_ctx(raw: str, *, session=None, args: str = "") -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=raw)
    return CommandContext(
        msg=msg, session=session, key=msg.session_key, raw=raw, args=args, loop=None,
    )


def _session(meta: dict | None = None):
    return SimpleNamespace(metadata=meta if meta is not None else {})


def _read_events(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l]


# ---------------------------------------------------------------------------
# Command palette / registry plumbing
# ---------------------------------------------------------------------------


class TestModeCommandsInPalette:

    def test_plan_in_palette(self):
        names = [s.command for s in BUILTIN_COMMAND_SPECS]
        assert "/plan" in names
        assert "/build" in names
        assert "/mode" in names

    def test_palette_returns_dicts_with_icons(self):
        palette = builtin_command_palette()
        plan_entry = next(p for p in palette if p["command"] == "/plan")
        assert plan_entry["icon"] == "lightbulb"
        assert plan_entry["title"]
        assert plan_entry["description"]

    def test_router_registers_mode_commands(self):
        router = CommandRouter()
        register_builtin_commands(router)
        assert router.is_dispatchable_command("/plan")
        assert router.is_dispatchable_command("/build")
        assert router.is_dispatchable_command("/mode")
        assert router.is_dispatchable_command("/mode plan")  # prefix form


# ---------------------------------------------------------------------------
# /plan
# ---------------------------------------------------------------------------


class TestCmdPlan:

    @pytest.mark.asyncio
    async def test_enters_plan_mode(self):
        session = _session({SESSION_MODE_KEY: "build"})
        result = await cmd_plan(_make_ctx("/plan", session=session))
        assert session.metadata[SESSION_MODE_KEY] == "plan"
        assert session.metadata[SESSION_PRE_PLAN_KEY] == "build"
        assert "PLAN MODE" in result.content

    @pytest.mark.asyncio
    async def test_idempotent_when_already_plan(self):
        session = _session({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        result = await cmd_plan(_make_ctx("/plan", session=session))
        assert session.metadata[SESSION_MODE_KEY] == "plan"
        assert "Already in PLAN" in result.content

    @pytest.mark.asyncio
    async def test_already_plan_with_args_explains_behavior_and_forwards(self):
        """When user types `/plan <task>` while already in plan, message
        should explain what happens and the task should still be forwarded."""
        from types import SimpleNamespace

        published: list = []

        class _FakeBus:
            async def publish_inbound(self, m):
                published.append(m)

        session = _session({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        loop = SimpleNamespace(bus=_FakeBus())
        msg = InboundMessage(
            channel="cli", sender_id="u1", chat_id="direct",
            content="/plan refine the plan",
        )
        ctx = CommandContext(
            msg=msg, session=session, key=msg.session_key,
            raw="/plan refine the plan", args="refine the plan", loop=loop,
        )

        result = await cmd_plan(ctx)
        assert "Already in PLAN MODE" in result.content
        # Should mention the user has options to refine or restart
        assert "refine" in result.content.lower() or "discard" in result.content.lower()
        # Task forwarded
        assert len(published) == 1
        assert published[0].content == "refine the plan"

    @pytest.mark.asyncio
    async def test_no_session_errors_gracefully(self):
        result = await cmd_plan(_make_ctx("/plan", session=None))
        assert "no active session" in result.content.lower()

    @pytest.mark.asyncio
    async def test_emits_switch_telemetry(self, tmp_path: Path):
        log_path = tmp_path / "tel.jsonl"
        logger = TelemetryLogger(log_path)
        session = _session({SESSION_MODE_KEY: "build"})

        token = bind_telemetry(logger)
        try:
            await cmd_plan(_make_ctx("/plan", session=session))
        finally:
            reset_telemetry(token)

        events = _read_events(log_path)
        switch_events = [e for e in events if e["type"] == "agent_mode.switch"]
        assert len(switch_events) == 1
        data = switch_events[0]["data"]
        assert data["from"] == "build"
        assert data["to"] == "plan"
        assert data["trigger"] == "slash_command"

    @pytest.mark.asyncio
    async def test_entering_plan_clears_stale_executing_path(self):
        """A new /plan supersedes any prior executing plan — clear it so
        autocompact doesn't keep re-injecting stale plan content."""
        session = _session({
            SESSION_MODE_KEY: "build",
            "executing_plan_path": "/old/plan.md",
        })
        await cmd_plan(_make_ctx("/plan", session=session))
        assert "executing_plan_path" not in session.metadata

    @pytest.mark.asyncio
    async def test_plan_with_args_forwards_task_to_bus(self):
        """`/plan <task>` activates mode AND re-publishes the task as a
        regular inbound message so the agent processes it in the same turn.
        Mirrors Claude Code's UX."""
        from dataclasses import dataclass, field
        from types import SimpleNamespace

        published: list = []

        class _FakeBus:
            async def publish_inbound(self, m):
                published.append(m)

        session = _session({SESSION_MODE_KEY: "build"})
        loop = SimpleNamespace(bus=_FakeBus())
        msg = InboundMessage(
            channel="cli", sender_id="u1", chat_id="direct",
            content="/plan refactor auth module",
        )
        ctx = CommandContext(
            msg=msg, session=session,
            key=msg.session_key, raw="/plan refactor auth module",
            args="refactor auth module", loop=loop,
        )

        result = await cmd_plan(ctx)

        # Mode switched
        assert session.metadata[SESSION_MODE_KEY] == "plan"
        # The slash command itself returns the activation announcement
        assert "PLAN MODE" in result.content
        # The task was re-published to the bus, NOT carrying the slash prefix
        assert len(published) == 1
        assert published[0].content == "refactor auth module"
        assert published[0].channel == "cli"
        # The CLI should keep the spinner running until the forwarded task
        # produces a response — otherwise the prompt returns and the user
        # has no signal that work is in flight.
        assert result.metadata.get("_block_input_until_response") is True

    @pytest.mark.asyncio
    async def test_plan_without_args_does_not_publish(self):
        """`/plan` alone activates the mode but does not re-publish anything."""
        from types import SimpleNamespace

        published: list = []

        class _FakeBus:
            async def publish_inbound(self, m):
                published.append(m)

        session = _session({SESSION_MODE_KEY: "build"})
        loop = SimpleNamespace(bus=_FakeBus())
        ctx = _make_ctx("/plan", session=session)
        ctx.loop = loop

        result = await cmd_plan(ctx)
        assert session.metadata[SESSION_MODE_KEY] == "plan"
        assert published == []
        # No background work scheduled, so the CLI should return to input
        # normally without the blocking flag.
        assert "_block_input_until_response" not in result.metadata


# ---------------------------------------------------------------------------
# /build
# ---------------------------------------------------------------------------


class TestCmdBuild:

    @pytest.mark.asyncio
    async def test_exits_plan_mode_restores_pre(self):
        session = _session({
            SESSION_MODE_KEY: "plan",
            SESSION_PRE_PLAN_KEY: "build",
        })
        result = await cmd_build(_make_ctx("/build", session=session))
        assert session.metadata[SESSION_MODE_KEY] == "build"
        assert SESSION_PRE_PLAN_KEY not in session.metadata
        assert "Exited plan mode" in result.content

    @pytest.mark.asyncio
    async def test_no_op_when_already_build(self):
        session = _session({SESSION_MODE_KEY: "build"})
        result = await cmd_build(_make_ctx("/build", session=session))
        assert "Already" in result.content

    @pytest.mark.asyncio
    async def test_from_explore_sets_to_build(self):
        session = _session({SESSION_MODE_KEY: "explore"})
        await cmd_build(_make_ctx("/build", session=session))
        assert session.metadata[SESSION_MODE_KEY] == "build"

    @pytest.mark.asyncio
    async def test_build_publishes_synthetic_trigger_to_bus(self):
        """/build approving a plan should wake the agent immediately by
        publishing a synthetic 'Proceed' message to the bus — without it,
        the model stays idle until the user types something."""
        from types import SimpleNamespace

        from durin.agent.tools.plan_mode import _ACTIVE_PLAN_PATH_KEY

        published: list = []

        class _FakeBus:
            async def publish_inbound(self, m):
                published.append(m)

        session = _session({
            SESSION_MODE_KEY: "plan",
            SESSION_PRE_PLAN_KEY: "build",
            _ACTIVE_PLAN_PATH_KEY: "/tmp/plan.md",
        })
        loop = SimpleNamespace(bus=_FakeBus())
        msg = InboundMessage(channel="cli", sender_id="u", chat_id="d", content="/build")
        ctx = CommandContext(msg=msg, session=session, key=msg.session_key, raw="/build", loop=loop)

        result = await cmd_build(ctx)
        assert "Exited plan mode" in result.content
        # The wake-up trigger should have been published
        assert len(published) == 1
        assert "approved plan" in published[0].content.lower()
        # Interactive CLIs use this flag to keep the spinner running and
        # hold the input prompt until the agent's follow-up response arrives.
        assert result.metadata.get("_block_input_until_response") is True

    @pytest.mark.asyncio
    async def test_build_with_no_plan_does_not_publish_trigger(self):
        """When /build runs without an approved plan path (e.g. just to
        switch modes), no synthetic trigger is needed."""
        from types import SimpleNamespace

        published: list = []

        class _FakeBus:
            async def publish_inbound(self, m):
                published.append(m)

        session = _session({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        loop = SimpleNamespace(bus=_FakeBus())
        msg = InboundMessage(channel="cli", sender_id="u", chat_id="d", content="/build")
        ctx = CommandContext(msg=msg, session=session, key=msg.session_key, raw="/build", loop=loop)

        result = await cmd_build(ctx)
        assert published == []
        # No background work scheduled → CLI should return to the input
        # prompt normally (no blocking flag).
        assert "_block_input_until_response" not in result.metadata

    @pytest.mark.asyncio
    async def test_handoff_plan_path_to_next_turn(self):
        """When /build approves a plan, the active_plan_path is migrated to
        approved_plan_path (one-shot reminder) AND executing_plan_path
        (persistent for autocompact)."""
        from durin.agent.tools.plan_mode import _ACTIVE_PLAN_PATH_KEY

        session = _session({
            SESSION_MODE_KEY: "plan",
            SESSION_PRE_PLAN_KEY: "build",
            _ACTIVE_PLAN_PATH_KEY: "/tmp/plan_abc.md",
        })
        result = await cmd_build(_make_ctx("/build", session=session))
        # Active plan key consumed (was for plan mode use)
        assert _ACTIVE_PLAN_PATH_KEY not in session.metadata
        # Approved plan key set for one-shot surfacing on next turn
        assert session.metadata["approved_plan_path"] == "/tmp/plan_abc.md"
        # Executing plan key set for autocompact re-injection
        assert session.metadata["executing_plan_path"] == "/tmp/plan_abc.md"
        assert "/tmp/plan_abc.md" in result.content

    @pytest.mark.asyncio
    async def test_no_plan_path_falls_back_gracefully(self):
        """If exit_plan_mode was never called, /build still works but does
        not surface a plan path."""
        session = _session({
            SESSION_MODE_KEY: "plan",
            SESSION_PRE_PLAN_KEY: "build",
        })
        result = await cmd_build(_make_ctx("/build", session=session))
        assert session.metadata[SESSION_MODE_KEY] == "build"
        assert "approved_plan_path" not in session.metadata
        assert "No plan file" in result.content


# ---------------------------------------------------------------------------
# /mode
# ---------------------------------------------------------------------------


class TestCmdMode:

    @pytest.mark.asyncio
    async def test_show_current_mode(self):
        session = _session({SESSION_MODE_KEY: "plan"})
        result = await cmd_mode(_make_ctx("/mode", session=session))
        assert "**plan**" in result.content
        assert "plan" in result.content and "build" in result.content

    @pytest.mark.asyncio
    async def test_set_mode_explicit(self):
        session = _session({SESSION_MODE_KEY: "build"})
        result = await cmd_mode(_make_ctx("/mode explore", session=session, args="explore"))
        assert session.metadata[SESSION_MODE_KEY] == "explore"
        assert "build" in result.content and "explore" in result.content

    @pytest.mark.asyncio
    async def test_set_mode_unknown(self):
        session = _session({SESSION_MODE_KEY: "build"})
        result = await cmd_mode(_make_ctx("/mode nonexistent", session=session, args="nonexistent"))
        assert "Unknown mode" in result.content
        assert session.metadata[SESSION_MODE_KEY] == "build"

    @pytest.mark.asyncio
    async def test_set_mode_to_same_is_noop(self):
        """`/mode build` while already in build shouldn't fake a transition."""
        session = _session({SESSION_MODE_KEY: "build"})
        result = await cmd_mode(_make_ctx("/mode build", session=session, args="build"))
        assert "Already in" in result.content
        assert "build" in result.content
