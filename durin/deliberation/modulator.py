"""Structural modulation — adjusts generator configs based on posture state.

Implements doc §4.2: the posture vector doesn't just bias scoring,
it changes WHICH generators fire, at what temperature, and with what permissions.
"""

from __future__ import annotations

from dataclasses import replace

from durin.deliberation.generator import GeneratorConfig
from durin.deliberation.types import GeneratorRole

_EXTRA_PERMISSION_EXPLORADOR = (
    "\n\nPodés cuestionar si la tarea tiene sentido o proponer no hacerla."
)

_DISCIPLINA_HIGH_SUFFIX = (
    "\n\nSeguí el procedimiento establecido. No improvises ni te desvíes del protocolo."
)


def modulate_generators(
    generators: list[GeneratorConfig],
    posture: dict[str, float],
) -> list[GeneratorConfig]:
    """Adjust generator list based on current posture snapshot.

    Structural rules (from doc §4.2):
    - Profundidad < 0.3: critico is omitted (shallow = skip deep analysis)
    - Exploración axis: explorador temperature shifts with exploration value
    - Conformidad < 0.3: explorador gets permission to question the task
    - Cautela > 0.7: extra pragmatico variant for more safe options
    - Cautela > 0.85: extra critico variant for maximum safety surface
    """
    cautela = posture.get("cautela", 0.5)
    exploracion = posture.get("exploracion", 0.5)
    profundidad = posture.get("profundidad", 0.5)
    conformidad = posture.get("conformidad", 0.5)

    result: list[GeneratorConfig] = []

    for gen in generators:
        if gen.role == GeneratorRole.CRITICO and profundidad < 0.3:
            continue

        adjusted = gen

        if gen.role == GeneratorRole.EXPLORADOR:
            new_temp = gen.temperature + 0.3 * (exploracion - 0.5)
            new_temp = max(0.5, min(1.2, new_temp))
            adjusted = replace(gen, temperature=new_temp)

            if conformidad < 0.3:
                adjusted = replace(
                    adjusted,
                    prompt_template=adjusted.prompt_template + _EXTRA_PERMISSION_EXPLORADOR,
                )

        result.append(adjusted)

    if cautela > 0.7:
        for gen in generators:
            if gen.role == GeneratorRole.PRAGMATICO:
                result.append(replace(gen, temperature=gen.temperature + 0.15))
                break

    if cautela > 0.85:
        for gen in generators:
            if gen.role == GeneratorRole.CRITICO:
                result.append(replace(gen, temperature=gen.temperature + 0.1))
                break

    disciplina = posture.get("disciplina", 0.5)

    if disciplina >= 0.6:
        result = [
            replace(g, prompt_template=g.prompt_template + _DISCIPLINA_HIGH_SUFFIX)
            for g in result
        ]

    if disciplina < 0.3:
        result = [
            replace(g, temperature=g.temperature + 0.1)
            if g.role == GeneratorRole.PRAGMATICO else g
            for g in result
        ]

    return result


def phrase_from_snapshot(posture: dict[str, float]) -> str:
    """Generate posture phrase from a snapshot dict (no PostureVector needed)."""
    phrases = {
        "cautela": {
            "low": "Asumí riesgo si avanza la tarea.",
            "high": "Priorizá reversibilidad. No rompas lo que funciona.",
        },
        "exploracion": {
            "low": "Usá lo conocido, no experimentes.",
            "high": "Considerá alternativas no obvias antes de actuar.",
        },
        "profundidad": {
            "low": "Sé directo, primera opción razonable.",
            "high": "Deliberá en profundidad antes de decidir.",
        },
        "disciplina": {
            "low": "Adaptá el procedimiento al contexto.",
            "high": "Seguí el protocolo establecido estrictamente.",
        },
        "conformidad": {
            "low": "Cuestioná si la tarea tiene sentido como está planteada.",
            "high": "Ejecutá lo pedido sin desvíos.",
        },
    }

    parts: list[str] = []
    for axis, buckets in phrases.items():
        value = posture.get(axis, 0.5)
        if value < 0.3:
            parts.append(buckets["low"])
        elif value >= 0.7:
            parts.append(buckets["high"])

    if not parts:
        return ""
    return "Postura actual: " + " ".join(parts)
