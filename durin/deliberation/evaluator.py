"""Evaluator — scores proposals on a specific dimension."""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass

from durin.deliberation.types import DeliberationContext, EvaluationScore, Proposal
from durin.providers.base import LLMProvider

_SCORE_RE = re.compile(r"(?:^|\s)(\d{1,2}(?:\.\d+)?)\b")
_FALLBACK_SCORE = 0.5
_SCALE_MAX = 10.0


class Evaluator(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @abc.abstractmethod
    async def evaluate(
        self,
        proposal: Proposal,
        context: DeliberationContext,
    ) -> EvaluationScore: ...


@dataclass(slots=True)
class LLMEvaluator(Evaluator):
    _name: str
    _provider: LLMProvider
    _model: str
    _prompt_template: str
    _max_tokens: int = 64
    _temperature: float = 0.0

    @property
    def name(self) -> str:
        return self._name

    async def evaluate(
        self,
        proposal: Proposal,
        context: DeliberationContext,
    ) -> EvaluationScore:
        user_content = (
            f"Objetivo: {context.goal_summary}\n\n"
            f"Propuesta ({proposal.role}): {proposal.content}"
        )
        response = await self._provider.chat(
            messages=[
                {"role": "system", "content": self._prompt_template},
                {"role": "user", "content": user_content},
            ],
            tools=None,
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        score, rationale = parse_score(response.content or "")
        return EvaluationScore(
            evaluator_name=self._name,
            score=score,
            rationale=rationale,
        )


def parse_score(text: str) -> tuple[float, str]:
    """Parse a score from evaluator output. Handles both 0-1 and 0-10 scales."""
    if not text.strip():
        return _FALLBACK_SCORE, ""
    match = _SCORE_RE.search(text)
    if not match:
        return _FALLBACK_SCORE, text.strip()
    raw = float(match.group(1))
    if raw > 1.0:
        raw = raw / _SCALE_MAX
    score = max(0.0, min(1.0, raw))
    remaining = text[match.end():].strip()
    return score, remaining
