"""Deliberation engine V3 — single-call multi-perspective with merge.

One LLM call generates Crítico → Explorador → Pragmático → Síntesis.
The ordering forces divergence: each perspective is conditioned on
what came before but NOT on the "obvious" solution (pragmático last).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from durin.deliberation.modulator import posture_modulation
from durin.deliberation.types import DeliberationContext, DeliberationResult, Perspective
from durin.providers.base import LLMProvider


_SYSTEM_PROMPT = """\
Sos un sistema de análisis multi-perspectiva. Examiná el problema desde 3 \
ángulos y luego sintetizá una recomendación.

IMPORTANTE: Cada perspectiva DEBE agregar información nueva. No repitas \
lo que ya se dijo en perspectivas anteriores.

{modulation}

Respondé EXACTAMENTE en este formato (usá los markers entre corchetes):

[CRITICO]
Identificá riesgos, supuestos no validados, errores potenciales. \
NO propongas soluciones — solo señalá problemas.

[EXPLORADOR]
Proponé un approach alternativo o no obvio. Debe ser diferente al camino \
directo. Considerá los riesgos ya identificados.

[PRAGMATICO]
El camino más directo y viable. Incorporá los riesgos válidos del crítico. \
Explicá por qué tu approach maneja esos riesgos.

[SINTESIS]
Merge final: qué hacer, incorporando lo válido de cada perspectiva. \
Si hay contradicción entre perspectivas, resolverla explícitamente."""


_USER_PROMPT = """\
Objetivo: {goal}

Contexto de investigación:
{context}"""

_USER_PROMPT_WITH_FAILURE = """\
Objetivo: {goal}

Contexto de investigación:
{context}

INTENTO PREVIO FALLIDO:
{failure}

Considerá por qué el intento anterior falló al generar tus perspectivas."""


_SECTION_PATTERN = re.compile(
    r"\[(?:CRITICO|EXPLORADOR|PRAGMATICO|SINTESIS)\]",
    re.IGNORECASE,
)

_MAX_CONTEXT_CHARS = 4000


@dataclass(slots=True)
class DeliberationEngine:
    """Single-call deliberation: 3 perspectives + merge in one LLM request."""

    provider: LLMProvider
    model: str
    temperature: float = 0.4
    max_tokens: int = 2048

    async def deliberate(self, context: DeliberationContext) -> DeliberationResult:
        """Run one LLM call that produces all perspectives and synthesis."""
        system_prompt = self._build_system_prompt(context)
        user_prompt = self._build_user_prompt(context)

        t0 = time.perf_counter()
        response = await self.provider.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=None,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        duration_ms = (time.perf_counter() - t0) * 1000

        result = self._parse_response(response.content or "")
        return DeliberationResult(
            perspectives=result.perspectives,
            synthesis=result.synthesis,
            duration_ms=duration_ms,
            model=self.model,
        )

    def _build_system_prompt(self, context: DeliberationContext) -> str:
        modulation = posture_modulation(context.posture_snapshot)
        return _SYSTEM_PROMPT.format(modulation=modulation)

    def _build_user_prompt(self, context: DeliberationContext) -> str:
        investigation = context.investigation_context[:_MAX_CONTEXT_CHARS]

        if context.previous_failure:
            return _USER_PROMPT_WITH_FAILURE.format(
                goal=context.goal_summary,
                context=investigation,
                failure=context.previous_failure[:1000],
            )
        return _USER_PROMPT.format(
            goal=context.goal_summary,
            context=investigation,
        )

    @staticmethod
    def _parse_response(content: str) -> DeliberationResult:
        """Parse structured output into perspectives + synthesis."""
        sections = _extract_sections(content)

        perspectives = []
        for role in ("critico", "explorador", "pragmatico"):
            text = sections.get(role, "").strip()
            if text:
                perspectives.append(Perspective(role=role, content=text))

        synthesis = sections.get("sintesis", "").strip()

        if not perspectives and not synthesis:
            return DeliberationResult(
                perspectives=(Perspective(role="pragmatico", content=content.strip()),),
                synthesis=content.strip(),
            )

        return DeliberationResult(
            perspectives=tuple(perspectives),
            synthesis=synthesis or (perspectives[-1].content if perspectives else ""),
        )


def _extract_sections(text: str) -> dict[str, str]:
    """Split text by [MARKER] sections into a dict."""
    markers = list(_SECTION_PATTERN.finditer(text))
    if not markers:
        return {}

    sections: dict[str, str] = {}
    for i, match in enumerate(markers):
        key = match.group(0).strip("[]").lower()
        start = match.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        sections[key] = text[start:end].strip()

    return sections
