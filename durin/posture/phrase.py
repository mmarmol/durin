"""Posture phrase generation — deterministic vector-to-text mapping."""

from __future__ import annotations

from enum import StrEnum

from durin.posture.vector import AxisName, PostureVector


class _Bucket(StrEnum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


_PHRASES: dict[tuple[AxisName, _Bucket], str] = {
    (AxisName.CAUTION, _Bucket.LOW): "Take risks if it advances the task.",
    (AxisName.CAUTION, _Bucket.HIGH): "Prioritize reversibility. Don't break what works.",
    (AxisName.EXPLORATION, _Bucket.LOW): "Use what's known, don't experiment.",
    (AxisName.EXPLORATION, _Bucket.HIGH): "Consider non-obvious alternatives before acting.",
    (AxisName.DEPTH, _Bucket.LOW): "Be direct, first reasonable option.",
    (AxisName.DEPTH, _Bucket.HIGH): "Deliberate in depth before deciding.",
    (AxisName.DISCIPLINE, _Bucket.LOW): "Adapt the procedure to context.",
    (AxisName.DISCIPLINE, _Bucket.HIGH): "Follow the established protocol strictly.",
    (AxisName.CONFORMITY, _Bucket.LOW): "Question whether the task makes sense as stated.",
    (AxisName.CONFORMITY, _Bucket.HIGH): "Execute what was requested without deviation.",
}


def _bucket_for(value: float) -> _Bucket:
    if value < 0.3:
        return _Bucket.LOW
    if value >= 0.7:
        return _Bucket.HIGH
    return _Bucket.MID


def generate_posture_phrase(vector: PostureVector) -> str:
    parts: list[str] = []
    for name, state in vector.axes.items():
        bucket = _bucket_for(state.current_value)
        if bucket == _Bucket.MID:
            continue
        phrase = _PHRASES.get((name, bucket))
        if phrase:
            parts.append(phrase)
    if not parts:
        return ""
    return "Current posture: " + " ".join(parts)
