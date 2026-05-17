"""Generator — SLM call to produce a proposal (seed or evolved)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from durin.deliberation.types import (
    DeliberationContext,
    GeneratorRole,
    Proposal,
    RoundResult,
)
from durin.providers.base import LLMProvider


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    role: GeneratorRole
    model: str
    temperature: float = 0.7
    max_tokens: int = 512
    prompt_template: str = ""


_SEED_SUFFIX = "Respondé en 1-3 oraciones con tu perspectiva desde tu rol."

_EVOLVE_INSTRUCTION = (
    "Refiná tu perspectiva. Incorporá lo valioso del ganador. "
    "Mejorá donde tu score fue bajo. Respondé en 2-3 oraciones."
)


async def generate_proposal(
    provider: LLMProvider,
    config: GeneratorConfig,
    context: DeliberationContext,
    round_number: int,
    posture_phrase: str = "",
    evolution_context: RoundResult | None = None,
) -> Proposal:
    if evolution_context is not None and round_number > 1:
        system_message = _build_system_prompt_evolve(config, posture_phrase, evolution_context)
        user_message = _build_user_prompt_evolve(context, config.role, evolution_context)
    else:
        system_message = _build_system_prompt_seed(config, posture_phrase)
        user_message = _build_user_prompt(context)

    response = await provider.chat(
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        tools=None,
        model=config.model,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
    )

    return Proposal(
        role=config.role,
        content=response.content or "",
        round_number=round_number,
        metadata={"usage": response.usage} if response.usage else {},
    )


def _build_system_prompt_seed(config: GeneratorConfig, posture_phrase: str) -> str:
    parts = []
    if config.prompt_template:
        parts.append(config.prompt_template)
    if posture_phrase:
        parts.append(posture_phrase)
    parts.append(_SEED_SUFFIX)
    return "\n\n".join(parts)


def _build_system_prompt_evolve(
    config: GeneratorConfig,
    posture_phrase: str,
    evolution: RoundResult,
) -> str:
    parts = []
    if config.prompt_template:
        parts.append(config.prompt_template)
    if posture_phrase:
        parts.append(posture_phrase)
    parts.append(_EVOLVE_INSTRUCTION)
    return "\n\n".join(parts)


def _build_user_prompt_evolve(
    context: DeliberationContext,
    role: GeneratorRole,
    evolution: RoundResult,
) -> str:
    parts = [f"Objetivo: {context.goal_summary}"]

    winner = evolution.winner
    parts.append(
        f"Propuesta ganadora ronda {evolution.round_number} "
        f"({winner.proposal.role}, score {winner.final_score:.0%}):\n"
        f'"{winner.proposal.content[:200]}"'
    )

    own_prev = _find_own_previous(role, evolution)
    if own_prev:
        parts.append(
            f"Tu propuesta anterior (score {own_prev.final_score:.0%}):\n"
            f'"{own_prev.proposal.content[:200]}"'
        )

    return "\n\n".join(parts)


def _find_own_previous(role: GeneratorRole, evolution: RoundResult) -> Any:
    for sp in evolution.proposals:
        if sp.proposal.role == role:
            return sp
    return None


def _build_user_prompt(context: DeliberationContext) -> str:
    parts = [f"Objetivo: {context.goal_summary}"]
    if context.active_objective:
        parts.append(f"Objetivo sostenido: {context.active_objective}")
    if context.conversation_summary:
        parts.append(f"Resumen reciente: {context.conversation_summary}")
    if context.previous_verdict_brief:
        parts.append(f"Decisión anterior: {context.previous_verdict_brief}")
    if context.recent_context:
        parts.append(f"Contexto: {context.recent_context}")
    return "\n\n".join(parts)
