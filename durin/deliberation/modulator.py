"""Posture modulation — adjusts deliberation prompt based on posture state."""

from __future__ import annotations


def posture_modulation(posture: dict[str, float]) -> str:
    """Generate prompt instructions based on current posture vector."""
    if not posture:
        return ""

    parts: list[str] = []

    caution = posture.get("caution", 0.6)
    if caution > 0.7:
        parts.append("High priority on the CRITIC: be exhaustive with risks and edge cases.")
    elif caution < 0.4:
        parts.append("The CRITIC can be brief if there are no obvious risks.")

    exploration = posture.get("exploration", 0.4)
    if exploration > 0.6:
        parts.append("The EXPLORER can propose radically different approaches.")

    depth = posture.get("depth", 0.5)
    if depth > 0.7:
        parts.append("Each perspective should be detailed (3-5 sentences).")
    else:
        parts.append("Each perspective should be concise (1-3 sentences).")

    return "\n".join(parts)
