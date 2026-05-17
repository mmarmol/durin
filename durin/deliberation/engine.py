"""Deliberation engine V3 — single-call multi-perspective with merge.

One LLM call generates Critic → Explorer → Pragmatic → Synthesis.
The ordering forces divergence: each perspective is conditioned on
what came before but NOT on the "obvious" solution (pragmatic last).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from durin.deliberation.modulator import posture_modulation
from durin.deliberation.types import DeliberationContext, DeliberationResult, Perspective
from durin.providers.base import LLMProvider


_SYSTEM_PROMPT = """\
You are a multi-perspective analysis system. Examine the problem from 3 \
angles and then synthesize a recommendation.

IMPORTANT: Each perspective MUST add new information. Do not repeat \
what was already said in previous perspectives.

{modulation}

Respond EXACTLY in this format (use the bracketed markers):

[CRITIC]
Identify risks, unvalidated assumptions, potential errors. \
Do NOT propose solutions — only flag problems.

[EXPLORER]
Propose an alternative or non-obvious approach. Must differ from the \
direct path. Consider the risks already identified.

[PRAGMATIC]
The most direct and viable path. Incorporate valid risks from the critic. \
Explain why your approach handles those risks.

[SYNTHESIS]
Final merge: what to do, incorporating what is valid from each perspective. \
If there is contradiction between perspectives, resolve it explicitly."""


_USER_PROMPT = """\
Goal: {goal}

Investigation context:
{context}"""

_USER_PROMPT_WITH_FAILURE = """\
Goal: {goal}

Investigation context:
{context}

PREVIOUS FAILED ATTEMPT:
{failure}

Consider why the previous attempt failed when generating your perspectives."""


_SECTION_PATTERN = re.compile(
    r"\[(?:CRITIC|EXPLORER|PRAGMATIC|SYNTHESIS)\]",
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
        for role in ("critic", "explorer", "pragmatic"):
            text = sections.get(role, "").strip()
            if text:
                perspectives.append(Perspective(role=role, content=text))

        synthesis = sections.get("synthesis", "").strip()

        if not perspectives and not synthesis:
            return DeliberationResult(
                perspectives=(Perspective(role="pragmatic", content=content.strip()),),
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
