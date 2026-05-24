"""LLM-as-judge for LoCoMo answers.

LoCoMo scoring requires semantic comparison — a correct answer "Yes,
on Tuesday" matches the ground truth "Tuesday" / "March 5" / "right
after the meeting" depending on the QA category. Exact-match misses
all of them. The paper uses GPT-4 as judge; we use glm-5.1 via the
durin provider config (z.ai coding plan, zero marginal cost).

The judge returns a :class:`JudgeVerdict` with:

- ``score``: 0.0 or 1.0 in v1 (binary; partial credit deferred — see
  doc 27 §5 for the rationale).
- ``reasoning``: 1–3 sentences explaining the score. This is the
  single most useful field for failure analysis — without it
  ``score == 0`` tells the user nothing actionable. Always written to
  the trace.
- ``confidence``: judge self-reported certainty 0-100. Low confidence
  flags the QA for manual review (the analyzer surfaces these as
  ``judge_error_possible``).

Markdown-marker format mirrors :mod:`durin.memory.absorb_judge` so
the parser surface is consistent across durin LLM-as-judge calls.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "JudgeError",
    "JudgeVerdict",
    "judge_answer",
]

logger = logging.getLogger(__name__)

LLMInvoke = Callable[..., str]


class JudgeError(RuntimeError):
    """Raised when the judge LLM call or output parsing fails after retries."""


@dataclass(frozen=True)
class JudgeVerdict:
    score: float        # 0.0 or 1.0 in v1
    confidence: int     # 0-100
    reasoning: str


_PROMPT_TEMPLATE = """Sos un evaluador de respuestas. Te paso una pregunta, la respuesta de referencia y la respuesta del agente. Tenés que decidir si la respuesta del agente es CORRECTA semánticamente — el wording puede variar mientras el contenido factual coincida con la referencia.

Pregunta: {question}

Respuesta de referencia (ground truth): {expected}

Respuesta del agente: {got}

Reglas:
- "Correcto" significa que un humano razonable diría "sí, el agente acertó". El wording, formato o nivel de detalle NO importan — solo el contenido factual.
- Para preguntas adversariales (preguntan algo NO discutido en la conversación), la respuesta correcta es declarar que no hay información. Si el agente alucinó una respuesta específica → INCORRECTO.
- Para preguntas temporales ("¿cuándo?"), una fecha aproximada que matchea la referencia es correcta. Una fecha incorrecta → INCORRECTO.
- Si el agente da una respuesta parcialmente correcta (parte sí, parte no), evaluá como INCORRECTO en v1 — preferimos precisión sobre recall.

Output exacto en este formato (sin texto extra antes/después):

===SCORE===
1 si correcto, 0 si incorrecto
===CONFIDENCE===
<entero 0-100>
===REASONING===
<1-3 oraciones explicando la decisión. Citá señales concretas.>
===END===
"""

_RE_SCORE = re.compile(r"===SCORE===\s*([01])\s*===CONFIDENCE===", re.IGNORECASE | re.DOTALL)
_RE_CONFIDENCE = re.compile(r"===CONFIDENCE===\s*(\d+)\s*===REASONING===", re.IGNORECASE | re.DOTALL)
_RE_REASONING = re.compile(r"===REASONING===\s*(.*?)\s*===END===", re.IGNORECASE | re.DOTALL)


def judge_answer(
    question: str,
    expected: str,
    got: str,
    *,
    llm_invoke: LLMInvoke,
    model: str = "glm-5.1",
    max_retries: int = 2,
) -> JudgeVerdict:
    """Score one (expected, got) pair and return the verdict.

    Empty ``got`` is a guaranteed miss without an LLM call — saves a
    judge invocation per timeout / exception. Same for an obvious
    refusal pattern.
    """
    if not got or not got.strip():
        return JudgeVerdict(
            score=0.0, confidence=100,
            reasoning="empty answer (agent produced no content)",
        )

    prompt = _PROMPT_TEMPLATE.format(
        question=question.strip(),
        expected=expected.strip(),
        got=got.strip(),
    )
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = llm_invoke(prompt, model=model)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "judge LLM call failed (attempt %d/%d): %s",
                attempt + 1, max_retries + 1, exc,
            )
            continue
        try:
            return _parse_verdict(raw)
        except JudgeError as exc:
            last_error = exc
            logger.warning(
                "judge parse failed (attempt %d/%d): %s",
                attempt + 1, max_retries + 1, exc,
            )
    raise JudgeError(
        f"judge failed after {max_retries + 1} attempts: {last_error}"
    )


def _parse_verdict(raw: str) -> JudgeVerdict:
    if not raw or not isinstance(raw, str):
        raise JudgeError("empty or non-string LLM response")

    score_match = _RE_SCORE.search(raw)
    if score_match is None:
        raise JudgeError("missing ===SCORE=== block")
    score = float(score_match.group(1))

    conf_match = _RE_CONFIDENCE.search(raw)
    if conf_match is None:
        raise JudgeError("missing ===CONFIDENCE=== block")
    try:
        confidence = int(conf_match.group(1).strip())
    except ValueError as exc:
        raise JudgeError(f"non-integer confidence: {exc}") from None
    if not 0 <= confidence <= 100:
        raise JudgeError(f"confidence {confidence} out of [0, 100]")

    reasoning_match = _RE_REASONING.search(raw)
    if reasoning_match is None:
        raise JudgeError("missing ===REASONING=== / ===END=== block")
    reasoning = reasoning_match.group(1).strip()
    if not reasoning:
        raise JudgeError("empty reasoning block")

    return JudgeVerdict(score=score, confidence=confidence, reasoning=reasoning)
