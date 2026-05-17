"""Posture vector session persistence and time-based decay."""

from __future__ import annotations

import math
import time
from typing import Any, Mapping, MutableMapping

from durin.posture.vector import AxisName, AxisState, PostureVector

POSTURE_METADATA_KEY = "posture_vector"
_DEFAULT_TAU_HOURS = 4.0


def serialize(vector: PostureVector) -> dict[str, Any]:
    return {
        "timestamp": time.time(),
        "axes": {
            name.value: {
                "media": state.media,
                "varianza": state.varianza,
                "fuerza_retorno": state.fuerza_retorno,
                "valor_actual": state.valor_actual,
            }
            for name, state in vector.axes.items()
        },
    }


def deserialize(data: dict[str, Any]) -> PostureVector:
    axes = {}
    for name in AxisName:
        axis_data = data["axes"][name.value]
        axes[name] = AxisState(
            media=axis_data["media"],
            varianza=axis_data["varianza"],
            fuerza_retorno=axis_data["fuerza_retorno"],
            valor_actual=axis_data["valor_actual"],
        )
    return PostureVector(axes=axes)


def apply_time_decay(
    vector: PostureVector,
    elapsed_seconds: float,
    tau_hours: float = _DEFAULT_TAU_HOURS,
) -> PostureVector:
    if elapsed_seconds <= 0:
        return vector
    factor = 1.0 - math.exp(-elapsed_seconds / (tau_hours * 3600.0))
    updates = {}
    for name, state in vector.axes.items():
        new_valor = state.valor_actual + factor * (state.media - state.valor_actual)
        updates[name] = state.model_copy(update={"valor_actual": new_valor})
    return vector.with_update(updates)


def save_posture(metadata: MutableMapping[str, Any], vector: PostureVector) -> None:
    metadata[POSTURE_METADATA_KEY] = serialize(vector)


def restore_posture(
    metadata: Mapping[str, Any],
    tau_hours: float = _DEFAULT_TAU_HOURS,
) -> PostureVector | None:
    data = metadata.get(POSTURE_METADATA_KEY)
    if not data:
        return None
    vector = deserialize(data)
    elapsed = time.time() - data.get("timestamp", time.time())
    if elapsed > 0:
        vector = apply_time_decay(vector, elapsed, tau_hours)
    return vector
