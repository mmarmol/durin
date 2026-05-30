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

    Audit H2 (2026-05-29) → H21 (2026-05-30): retry budget hardened
    via 5 attempts + exponential backoff. The judge always calls at
    ``temperature=0`` for score reproducibility — the earlier H2
    design varied temperature on retries to "break LLM wedges", but
    that was speculation and broke run-to-run determinism (a re-judge
    of the same trace could score differently). For a JUDGE,
    determinism > variance: if upstream is genuinely wedged at temp=0,
    backoff + a fresh provider window is the right cure, not jitter.
    """
    if not got or not got.strip():
        return JudgeVerdict(
            score=0.0, confidence=100,
            reasoning="empty answer (agent produced no content)",
        )

    # Audit H14 (2026-05-29) → H20 (2026-05-30): adversarial QAs with
    # the ``__REFUSE__`` sentinel use the LLM judge with a refusal-
    # specific prompt instead of lexical substring matching. The
    # original H14 missed "stealth refusals" like *"it was actually X,
    # not Y"* (3 of 11 fails on bench-100-prop were valid refusals
    # the lexical matcher couldn't catch). H20 reuses the existing
    # judge LLM infrastructure (retries / backoff / temperature
    # jitter via judge_answer's machinery) — no separate retry budget
    # needed because the judge function the caller already passed in
    # carries that policy.
    if expected.strip() == "__REFUSE__":
        return _score_refusal_llm(
            question=question, got=got,
            llm_invoke=llm_invoke, model=model,
            max_retries=max_retries,
        )

    prompt = _PROMPT_TEMPLATE.format(
        question=question.strip(),
        expected=expected.strip(),
        got=got.strip(),
    )
    accepts_temperature = _invoke_accepts_temperature(llm_invoke)
    last_error: Exception | None = None
    total_attempts = max_retries + 1
    # H21 (2026-05-30): always temp=0 across attempts. Earlier H2
    # design varied temperature per attempt; that broke score
    # reproducibility (re-judge of the same trace could differ) and
    # the original justification (breaking LLM wedges) was
    # speculation not measurement.
    JUDGE_TEMPERATURE = 0.0
    for attempt in range(total_attempts):
        try:
            if accepts_temperature:
                raw = llm_invoke(
                    prompt, model=model, temperature=JUDGE_TEMPERATURE,
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


_REFUSAL_JUDGE_PROMPT = """You are evaluating whether an AI agent correctly refused to answer an adversarial memory-recall question.

ADVERSARIAL questions presuppose something FALSE — they ask about an event or fact that did NOT happen in the conversation the agent has access to. The CORRECT behavior is to REFUSE the false premise. Refusal can take ANY of these forms:
- Explicitly say "I don't have info" / "no record" / equivalent in any language
- Correct the false presupposition: "it was actually X (not Y) who did Z"
- Point out the question's premise is wrong: "Y did not do Z"
- Say the entity in question didn't do the action; attribute the action to whoever actually did it

WRONG behavior (hallucination):
- Affirmatively claim the false fact is true
- Provide fabricated details about an event that didn't happen
- Invent specifics that aren't in the conversation

Question: {question}

Agent answer:
{got}

Return JSON with two fields:
  "refused": true if the agent refused in any form, false if it hallucinated a positive answer
  "reasoning": one short sentence explaining

Output ONLY the JSON object, no markdown wrapping, no other text."""


# Legacy H14 lexical markers (kept for offline / batch re-judging when
# the LLM judge is unavailable). Pre-H20 default; H20 replaces with
# ``_score_refusal_llm``.
_REFUSAL_MARKERS = (
    "i don't have",
    "i do not have",
    "no information",
    "no record",
    "not specified",
    "not mentioned",
    "cannot determine",
    "can't determine",
    "unable to determine",
    "i don't know",
    "no info",
    "the memory doesn't",
    "the memory does not",
    "no encuentro",
    "no tengo información",
    "no tengo informacion",
    "no se menciona",
    "no se especifica",
    "no hay registro",
)


def _score_refusal_llm(
    *,
    question: str,
    got: str,
    llm_invoke: LLMInvoke,
    model: str,
    max_retries: int,
) -> "JudgeVerdict":
    """H20 (2026-05-30): adversarial refusal rubric via LLM judge.

    Uses the same retry / backoff / temperature-jitter loop as the
    main judge (delegates to the existing pattern with a refusal-
    specific prompt). Adversarial QAs typically take agents through
    stealth-refusal phrasings ("it was actually X, not Y") that no
    lexical matcher can cover across languages without an
    ever-growing list of patterns. LLM judge handles it naturally.
    """
    import json as _json

    accepts_temperature = _invoke_accepts_temperature(llm_invoke)
    prompt = _REFUSAL_JUDGE_PROMPT.format(question=question, got=got)
    last_error: object = None
    total_attempts = max_retries + 1
    # H21 (2026-05-30): refusal judge also runs at temp=0 for score
    # reproducibility — same rationale as the main judge_answer loop.
    JUDGE_TEMPERATURE = 0.0
    for attempt in range(total_attempts):
        try:
            if accepts_temperature:
                raw = llm_invoke(
                    prompt, model=model, temperature=JUDGE_TEMPERATURE,
                )
            else:
                raw = llm_invoke(prompt, model=model)
        except TypeError:
            accepts_temperature = False
            try:
                raw = llm_invoke(prompt, model=model)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                _maybe_sleep_backoff(attempt, total_attempts)
                continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _maybe_sleep_backoff(attempt, total_attempts)
            continue
        text = _coerce_to_text(raw)
        if not isinstance(text, str) or not text.strip():
            last_error = "empty refusal-judge response"
            _maybe_sleep_backoff(attempt, total_attempts)
            continue
        # Best-effort JSON parse: tolerate markdown fences if the
        # model wraps the output despite the prompt.
        body = text.strip()
        if body.startswith("```"):
            body = body.strip("`")
            if body.lower().startswith("json"):
                body = body[4:]
            body = body.strip()
        try:
            parsed = _json.loads(body)
        except _json.JSONDecodeError:
            # Try to extract the first JSON object embedded in prose.
            import re as _re
            m = _re.search(r"\{.*\}", body, _re.DOTALL)
            if not m:
                last_error = f"unparseable refusal-judge response: {text[:120]!r}"
                _maybe_sleep_backoff(attempt, total_attempts)
                continue
            try:
                parsed = _json.loads(m.group(0))
            except _json.JSONDecodeError as exc:
                last_error = f"json error: {exc}"
                _maybe_sleep_backoff(attempt, total_attempts)
                continue
        refused = bool(parsed.get("refused"))
        reasoning = str(parsed.get("reasoning") or "(no reasoning)")
        return JudgeVerdict(
            score=1.0 if refused else 0.0,
            confidence=90,
            reasoning=(
                ("adversarial: refusal detected — " if refused
                 else "adversarial: hallucinated positive answer — ")
                + reasoning[:160]
            ),
        )
    # All attempts failed — fall back to the lexical legacy matcher
    # so we don't lose total signal on this QA.
    fallback = _score_refusal_lexical(got)
    return JudgeVerdict(
        score=fallback.score,
        confidence=50,
        reasoning=(
            f"refusal-judge failed after {total_attempts} attempts "
            f"({last_error}); lexical fallback → {fallback.reasoning}"
        ),
    )


def _score_refusal_lexical(got: str) -> "JudgeVerdict":
    """H14: adversarial refusal rubric — agent earns 1.0 when its
    answer expresses 'I don't have / no info / cannot determine'
    rather than hallucinating a value."""
    low = got.lower()
    if any(marker in low for marker in _REFUSAL_MARKERS):
        return JudgeVerdict(
            score=1.0, confidence=95,
            reasoning="adversarial: agent correctly refused / said no info",
        )
    return JudgeVerdict(
        score=0.0, confidence=80,
        reasoning=(
            "adversarial: agent gave a positive answer when the gold "
            "expected a refusal — likely hallucinated a fact not in the "
            "conversation"
        ),
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

    Sleeps ``4 * 2 ** attempt`` seconds after attempt N (0-indexed),
    but skips the sleep that would come after the very last attempt
    (no point waiting just to raise). The schedule for the default
    5-attempt budget is 4, 8, 16, 32 — 60 s total worst case.

    Audit H8 (2026-05-29): base bumped from 1 s to 4 s after the
    bench-100 overnight run hit z.ai outage windows lasting hours.
    The original 1-2-4-8 schedule (15 s worst case) burnt through
    every retry before z.ai recovered; the 4-8-16-32 schedule gives
    upstream more headroom before the next attempt without changing
    the retry budget itself.
    """
    if attempt + 1 >= total_attempts:
        return
    time.sleep(float(4 * (2 ** attempt)))


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
