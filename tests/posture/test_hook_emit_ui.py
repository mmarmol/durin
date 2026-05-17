"""Tests for PostureHook emit_ui integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from durin.agent.hook import AgentHookContext
from durin.posture.hook import PostureHook
from durin.posture.stimulus import StimulusEvent
from durin.posture.vector import PostureVector
from durin.providers.base import ToolCallRequest


def _make_context(iteration: int = 0, emit_ui=None, **kwargs) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=[],
        emit_ui=emit_ui,
        **kwargs,
    )


class TestPostureHookEmitUI:
    @pytest.mark.asyncio
    async def test_emits_initial_posture_on_first_iteration(self):
        emit = AsyncMock()
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(iteration=0, emit_ui=emit)

        await hook.before_iteration(ctx)

        emit.assert_called_once()
        kind, data = emit.call_args[0]
        assert kind == "posture_update"
        assert "caution" in data["axes"]
        assert data["deltas"] == {}

    @pytest.mark.asyncio
    async def test_does_not_emit_on_non_first_iteration(self):
        emit = AsyncMock()
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(iteration=1, emit_ui=emit)

        await hook.before_iteration(ctx)

        emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_delta_after_posture_change(self):
        emit = AsyncMock()
        hook = PostureHook(PostureVector.default())
        tc = ToolCallRequest(id="1", name="test", arguments={})
        ctx = _make_context(
            iteration=0,
            emit_ui=emit,
            tool_calls=[tc],
            tool_results=[{"error": "something failed"}],
            error="test error",
        )

        await hook.after_iteration(ctx)

        assert emit.call_count == 1
        kind, data = emit.call_args[0]
        assert kind == "posture_update"
        assert any(abs(v) > 0 for v in data["deltas"].values())

    @pytest.mark.asyncio
    async def test_no_emit_when_no_emit_ui_callback(self):
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(iteration=0, emit_ui=None)
        await hook.before_iteration(ctx)

    @pytest.mark.asyncio
    async def test_no_emit_when_no_change(self):
        emit = AsyncMock()
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(iteration=1, emit_ui=emit, final_content="some text")

        await hook.after_iteration(ctx)

        emit.assert_not_called()
