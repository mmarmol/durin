"""Homeostasis update functions — pure, stateless, deterministic."""

from __future__ import annotations

from durin.posture.vector import AxisName, AxisState, PostureVector

_REFERENCE_VARIANCE = 0.15


def apply_return_to_mean(state: AxisState) -> AxisState:
    new_value = state.current_value + state.return_force * (state.mean - state.current_value)
    return state.model_copy(update={"current_value": new_value})


def apply_stimulus(state: AxisState, delta: float) -> AxisState:
    normalized = delta * (state.variance / _REFERENCE_VARIANCE)
    new_value = state.current_value + normalized
    return state.model_copy(update={"current_value": new_value})


def apply_clamp(state: AxisState) -> AxisState:
    lower = max(0.0, state.mean - 2 * state.variance)
    upper = min(1.0, state.mean + 2 * state.variance)
    clamped = max(lower, min(upper, state.current_value))
    return state.model_copy(update={"current_value": clamped})


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
