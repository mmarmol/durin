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

import inspect
import logging
import re
import time
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
    max_retries: int = 4,
) -> JudgeVerdict:
    """Score one (expected, got) pair and return the verdict.

    Empty ``got`` is a guaranteed miss without an LLM call — saves a
    judge invocation per timeout / exception. Same for an obvious
    refusal pattern.

    Audit H2 (2026-05-29): retry budget hardened. Previously
    ``max_retries=2`` with no backoff and constant temperature meant
    a ~30s upstream outage (z.ai returning empty completions) burned
    all 3 attempts in <5s and the QA was marked
    ``judge_error_possible``. H2 raises the default to 4 retries
    (5 attempts total), adds exponential backoff (1, 2, 4, 8 s) so
    the loop survives a transient outage, and varies temperature per
    attempt (0.0, 0.2, 0.4, 0.6, 0.8) to break upstream wedges where
    temp=0 reliably returns the same broken completion. Callers that
    rely on the old budget for reproducibility can pin
    ``max_retries=2``.
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
    accepts_temperature = _invoke_accepts_temperature(llm_invoke)
    last_error: Exception | None = None
    total_attempts = max_retries + 1
    for attempt in range(total_attempts):
        # H2: temperature jitter starts at 0.0 and steps by +0.2 per
        # retry. Cap at 1.0 in case the budget grows past 5 attempts.
        # ``round`` avoids float-binary surprises (0.2*3=0.6000…01).
        temperature = round(min(1.0, 0.2 * attempt), 4)
        try:
            if accepts_temperature:
                raw = llm_invoke(
                    prompt, model=model, temperature=temperature,
                )
            else:
                raw = llm_invoke(prompt, model=model)
        except TypeError:
            # Defensive: an invoker may accept `temperature` in its
            # signature but reject the value (e.g. typing mismatch).
            # Fall back to the temp-less call and remember to skip
            # the kwarg on subsequent attempts.
            accepts_temperature = False
            try:
                raw = llm_invoke(prompt, model=model)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "judge LLM call failed (attempt %d/%d): %s",
                    attempt + 1, total_attempts, exc,
                )
                _maybe_sleep_backoff(attempt, total_attempts)
                continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "judge LLM call failed (attempt %d/%d): %s",
                attempt + 1, total_attempts, exc,
            )
            _maybe_sleep_backoff(attempt, total_attempts)
            continue
        try:
            return _parse_verdict(_coerce_to_text(raw))
        except JudgeError as exc:
            last_error = exc
            logger.warning(
                "judge parse failed (attempt %d/%d): %s",
                attempt + 1, total_attempts, exc,
            )
        _maybe_sleep_backoff(attempt, total_attempts)
    raise JudgeError(
        f"judge failed after {total_attempts} attempts: {last_error}"
    )


def _coerce_to_text(raw: object) -> object:
    """Unwrap LLM response objects to their textual payload.

    The :class:`LLMInvoke` protocol declares ``str`` return values,
    but the production :func:`durin.memory.dream.default_llm_invoke`
    returns an :class:`LLMResponse` dataclass carrying both text and
    token counts. Without unwrapping, every judge call against the
    real invoker fails the ``isinstance(raw, str)`` check inside
    :func:`_parse_verdict` and retries until exhausted — verified
    by the 2026-05-29 bench (5/5 QAs reported
    ``judge_error_possible`` despite the agent producing correct
    answers in 3 of them).

    Tolerant unwrap: any object with a ``.text`` attribute (the
    LLMResponse convention) gives up its text; everything else passes
    through unchanged so the existing parser path can reject it with
    a clear ``JudgeError``.
    """
    if isinstance(raw, str):
        return raw
    text = getattr(raw, "text", None)
    if isinstance(text, str):
        return text
    return raw


def _maybe_sleep_backoff(attempt: int, total_attempts: int) -> None:
    """Exponential backoff between judge attempts.

    Sleeps ``2 ** attempt`` seconds after attempt N (0-indexed), but
    skips the sleep that would come after the very last attempt (no
    point waiting just to raise). The schedule for the default
    5-attempt budget is 1, 2, 4, 8 — 15 s total worst case, enough
    to ride out the kind of upstream outage observed on 2026-05-29.
    """
    if attempt + 1 >= total_attempts:
        return
    time.sleep(float(2 ** attempt))


def _invoke_accepts_temperature(llm_invoke: LLMInvoke) -> bool:
    """Best-effort probe: does ``llm_invoke`` accept ``temperature``?

    The :class:`LLMInvoke` protocol historically was ``(prompt, *,
    model)``; H2 wants to pass ``temperature`` too without breaking
    callers stuck on the old signature. A strict ``inspect`` check
    keeps the judge tolerant — for unknown callables (C-extensions
    without a signature) we assume yes and fall back to a temp-less
    call inside the loop if it raises ``TypeError``.
    """
    try:
        sig = inspect.signature(llm_invoke)
    except (TypeError, ValueError):
        return True
    params = sig.parameters
    if "temperature" in params:
        return True
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
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
