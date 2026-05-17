"""Posture modulation — adjusts deliberation prompt based on posture state."""

from __future__ import annotations


def posture_modulation(posture: dict[str, float]) -> str:
    """Generate prompt instructions based on current posture vector."""
    if not posture:
        return ""

    parts: list[str] = []

    cautela = posture.get("cautela", 0.6)
    if cautela > 0.7:
        parts.append("Prioridad alta en el CRITICO: sé exhaustivo con riesgos y edge cases.")
    elif cautela < 0.4:
        parts.append("El CRITICO puede ser breve si no hay riesgos obvios.")

    exploracion = posture.get("exploracion", 0.4)
    if exploracion > 0.6:
        parts.append("El EXPLORADOR puede proponer approaches radicalmente diferentes.")

    profundidad = posture.get("profundidad", 0.5)
    if profundidad > 0.7:
        parts.append("Cada perspectiva debe ser detallada (3-5 oraciones).")
    else:
        parts.append("Cada perspectiva debe ser concisa (1-3 oraciones).")

    return "\n".join(parts)
