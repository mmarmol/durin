"""End-to-end integration test for the posture system."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.agent.hook import AgentHookContext, CompositeHook
from durin.config.schema import AgentDefaults, AxisConfig, PostureConfig
from durin.posture.hook import PostureHook
from durin.posture.persistence import restore_posture, save_posture
from durin.posture.phrase import generate_posture_phrase
from durin.posture.stimulus import StimulusTable
from durin.posture.vector import AxisName, AxisState, PostureVector
from durin.providers.base import ToolCallRequest


def _make_context(
    *,
    iteration: int = 0,
    error: str | None = None,
    tool_calls: list | None = None,
    tool_results: list | None = None,
) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=[],
        error=error,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
    )


def _tool_call(name: str = "test") -> ToolCallRequest:
    return ToolCallRequest(id="tc_1", name=name, arguments="{}")


class TestFullLifecycle:
    """Proves the complete posture lifecycle works end-to-end."""

    @pytest.mark.asyncio
    async def test_failures_increase_cautela_change_phrase(self):
        vector = PostureVector.default()
        hook = PostureHook(vector)

        initial_phrase = hook.current_phrase
        initial_cautela = hook.current_vector.axes[AxisName.CAUTELA].valor_actual

        for i in range(5):
            ctx = _make_context(iteration=i, error="tool execution failed")
            await hook.after_iteration(ctx)

        assert hook.current_vector.axes[AxisName.CAUTELA].valor_actual > initial_cautela
        final_phrase = hook.current_phrase
        assert "reversibilidad" in final_phrase

    @pytest.mark.asyncio
    async def test_successes_decrease_cautela(self):
        axes = {}
        for name in AxisName:
            default_state = PostureVector.default().axes[name]
            axes[name] = default_state.model_copy(update={"valor_actual": 0.8})
        vector = PostureVector(axes=axes)
        hook = PostureHook(vector)

        for i in range(5):
            ctx = _make_context(
                iteration=i,
                tool_calls=[_tool_call()],
                tool_results=[{"output": "success"}],
            )
            await hook.after_iteration(ctx)

        assert hook.current_vector.axes[AxisName.CAUTELA].valor_actual < 0.8

    @pytest.mark.asyncio
    async def test_persistence_survives_session_restart(self):
        vector = PostureVector.default()
        hook = PostureHook(vector)

        for i in range(3):
            ctx = _make_context(iteration=i, error="crash")
            await hook.after_iteration(ctx)

        metadata: dict = {}
        save_posture(metadata, hook.current_vector)

        saved_ts = metadata["posture_vector"]["timestamp"]
        with patch("durin.posture.persistence.time.time", return_value=saved_ts):
            restored = restore_posture(metadata)

        assert restored is not None
        for name in AxisName:
            assert restored.axes[name].valor_actual == pytest.approx(
                hook.current_vector.axes[name].valor_actual,
            )

    @pytest.mark.asyncio
    async def test_time_decay_after_restore(self):
        vector = PostureVector.default()
        hook = PostureHook(vector)

        for i in range(5):
            ctx = _make_context(iteration=i, error="crash")
            await hook.after_iteration(ctx)

        metadata: dict = {}
        save_posture(metadata, hook.current_vector)

        four_hours_later = metadata["posture_vector"]["timestamp"] + 4 * 3600
        with patch("durin.posture.persistence.time.time", return_value=four_hours_later):
            restored = restore_posture(metadata, tau_hours=4.0)

        assert restored is not None
        for name in AxisName:
            before = hook.current_vector.axes[name].valor_actual
            after = restored.axes[name].valor_actual
            media = restored.axes[name].media
            if abs(before - media) > 0.01:
                assert abs(after - media) < abs(before - media)

    @pytest.mark.asyncio
    async def test_config_disabled_no_posture_behavior(self):
        config = PostureConfig(enabled=False)
        assert config.enabled is False

    def test_config_to_vector_creation(self):
        config = PostureConfig(
            enabled=True,
            axes={
                "cautela": AxisConfig(media=0.7, varianza=0.2, fuerza_retorno=0.4),
                "exploracion": AxisConfig(media=0.3, varianza=0.1, fuerza_retorno=0.5),
                "profundidad": AxisConfig(media=0.5, varianza=0.15, fuerza_retorno=0.3),
                "disciplina": AxisConfig(media=0.5, varianza=0.15, fuerza_retorno=0.3),
                "conformidad": AxisConfig(media=0.6, varianza=0.15, fuerza_retorno=0.3),
            },
        )
        axes = {}
        for name in AxisName:
            ac = config.axes[name.value]
            axes[name] = AxisState(
                media=ac.media,
                varianza=ac.varianza,
                fuerza_retorno=ac.fuerza_retorno,
                valor_actual=ac.media,
            )
        vector = PostureVector(axes=axes)
        assert vector.axes[AxisName.CAUTELA].media == 0.7
        assert vector.axes[AxisName.EXPLORACION].media == 0.3

    @pytest.mark.asyncio
    async def test_composite_hook_with_posture_multiple_iterations(self):
        hook = PostureHook(PostureVector.default())
        composite = CompositeHook([hook])

        for i in range(3):
            ctx = _make_context(iteration=i, error="fail")
            await composite.after_iteration(ctx)

        for i in range(3, 8):
            ctx = _make_context(
                iteration=i,
                tool_calls=[_tool_call()],
                tool_results=[{"output": "ok"}],
            )
            await composite.after_iteration(ctx)

        cautela_default = PostureVector.default().axes[AxisName.CAUTELA].valor_actual
        assert hook.current_vector.axes[AxisName.CAUTELA].valor_actual != cautela_default

    @pytest.mark.asyncio
    async def test_phrase_changes_with_vector_evolution(self):
        vector = PostureVector.default()
        hook = PostureHook(vector)

        phrases_seen: set[str] = set()
        phrases_seen.add(hook.current_phrase)

        for i in range(10):
            ctx = _make_context(iteration=i, error="fail")
            await hook.after_iteration(ctx)
            phrases_seen.add(hook.current_phrase)

        assert len(phrases_seen) >= 2

    def test_all_mid_vector_produces_no_phrase(self):
        axes = {}
        for name in AxisName:
            axes[name] = AxisState(
                media=0.5, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.5,
            )
        vector = PostureVector(axes=axes)
        assert generate_posture_phrase(vector) == ""
