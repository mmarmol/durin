"""Homeostasis update functions — pure, stateless, deterministic."""

from __future__ import annotations

from durin.posture.vector import AxisName, AxisState, PostureVector

_REFERENCE_VARIANZA = 0.15


def apply_return_to_mean(state: AxisState) -> AxisState:
    new_valor = state.valor_actual + state.fuerza_retorno * (state.media - state.valor_actual)
    return state.model_copy(update={"valor_actual": new_valor})


def apply_stimulus(state: AxisState, delta: float) -> AxisState:
    normalized = delta * (state.varianza / _REFERENCE_VARIANZA)
    new_valor = state.valor_actual + normalized
    return state.model_copy(update={"valor_actual": new_valor})


def apply_clamp(state: AxisState) -> AxisState:
    lower = max(0.0, state.media - 2 * state.varianza)
    upper = min(1.0, state.media + 2 * state.varianza)
    clamped = max(lower, min(upper, state.valor_actual))
    return state.model_copy(update={"valor_actual": clamped})


def update_axis(state: AxisState, delta: float) -> AxisState:
    state = apply_return_to_mean(state)
    state = apply_stimulus(state, delta)
    state = apply_clamp(state)
    return state


def update_vector(vector: PostureVector, deltas: dict[AxisName, float]) -> PostureVector:
    updates: dict[AxisName, AxisState] = {}
    for name, state in vector.axes.items():
        delta = deltas.get(name, 0.0)
        updates[name] = update_axis(state, delta)
    return PostureVector(axes=updates)
