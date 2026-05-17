"""Pure scoring functions — no I/O, no state, fully deterministic."""

from __future__ import annotations


def compute_weight_avance(cautela: float) -> float:
    return 0.5 - 0.4 * (cautela - 0.5)


def compute_weight_reversibilidad(cautela: float) -> float:
    return 0.5 + 0.4 * (cautela - 0.5)


def compute_final_score(
    avance: float,
    reversibilidad: float,
    cautela: float,
) -> float:
    w_a = compute_weight_avance(cautela)
    w_r = compute_weight_reversibilidad(cautela)
    return w_a * avance + w_r * reversibilidad


def compute_threshold(profundidad: float) -> float:
    return 0.4 + 0.3 * profundidad
