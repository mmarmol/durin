"""Posture phrase generation — deterministic vector-to-text mapping."""

from __future__ import annotations

from enum import StrEnum

from durin.posture.vector import AxisName, PostureVector


class _Bucket(StrEnum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


_PHRASES: dict[tuple[AxisName, _Bucket], str] = {
    (AxisName.CAUTELA, _Bucket.LOW): "Asumí riesgo si avanza la tarea.",
    (AxisName.CAUTELA, _Bucket.HIGH): "Priorizá reversibilidad. No rompas lo que funciona.",
    (AxisName.EXPLORACION, _Bucket.LOW): "Usá lo conocido, no experimentes.",
    (AxisName.EXPLORACION, _Bucket.HIGH): "Considerá alternativas no obvias antes de actuar.",
    (AxisName.PROFUNDIDAD, _Bucket.LOW): "Sé directo, primera opción razonable.",
    (AxisName.PROFUNDIDAD, _Bucket.HIGH): "Deliberá en profundidad antes de decidir.",
    (AxisName.DISCIPLINA, _Bucket.LOW): "Adaptá el procedimiento al contexto.",
    (AxisName.DISCIPLINA, _Bucket.HIGH): "Seguí el protocolo establecido estrictamente.",
    (AxisName.CONFORMIDAD, _Bucket.LOW): "Cuestioná si la tarea tiene sentido como está planteada.",
    (AxisName.CONFORMIDAD, _Bucket.HIGH): "Ejecutá lo pedido sin desvíos.",
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
        bucket = _bucket_for(state.valor_actual)
        if bucket == _Bucket.MID:
            continue
        phrase = _PHRASES.get((name, bucket))
        if phrase:
            parts.append(phrase)
    if not parts:
        return ""
    return "Postura actual: " + " ".join(parts)
