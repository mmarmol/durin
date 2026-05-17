"""Engine — generates diverse perspectives for enrichment injection.

V2: No evaluators, no scoring, no multi-round evolution.
Generates 3 perspectives in parallel (1 LLM call each) and packages them
directly as enrichment context for the main model.

The main model is the best evaluator — it has full context. Our job is
only to provide diverse starting points it wouldn't generate on its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger

from durin.deliberation.generator import GeneratorConfig, generate_proposal
from durin.deliberation.modulator import modulate_generators, phrase_from_snapshot
from durin.deliberation.types import (
    ConvergenceReason,
    DeliberationContext,
    EvaluationScore,
    GeneratorRole,
    Proposal,
    RoundResult,
    ScoredProposal,
    Verdict,
)
from durin.providers.base import LLMProvider


@dataclass(slots=True)
class DeliberationEngine:
    provider: LLMProvider
    generators: list[GeneratorConfig]
    evaluators: list  # kept for interface compat, not used in v2
    max_rounds: int = 1
    posture_phrase: str = ""

    async def deliberate(self, context: DeliberationContext) -> Verdict:
        """Generate diverse perspectives and return as a Verdict.

        V2 always runs a single round — no evaluation, no scoring loop.
        All proposals get a neutral score; the 'winner' is the pragmatic one
        by convention (the main model sees all three anyway).
        """
        active_generators = modulate_generators(self.generators, context.posture_snapshot)
        active_phrase = (
            phrase_from_snapshot(context.posture_snapshot)
            if context.posture_snapshot
            else self.posture_phrase
        )

        proposals = await self._generate_perspectives(
            context, generators=active_generators, phrase=active_phrase,
        )

        if not proposals:
            logger.warning("All generators failed")
            return self._empty_verdict()

        scored = tuple(
            ScoredProposal(proposal=p, scores=(), final_score=0.5)
            for p in proposals
        )

        # By convention, pragmatico is the "winner" for synthesis compatibility
        winner = next(
            (sp for sp in scored if sp.proposal.role == GeneratorRole.PRAGMATICO),
            scored[0],
        )

        return Verdict(
            winner=winner,
            accepted=True,
            threshold=0.5,
            all_proposals=scored,
            rounds_used=1,
            under_doubt=False,
            convergence_reason=ConvergenceReason.THRESHOLD,
        )

    async def _generate_perspectives(
        self,
        context: DeliberationContext,
        *,
        generators: list[GeneratorConfig] | None = None,
        phrase: str = "",
    ) -> list[Proposal]:
        active = generators if generators is not None else self.generators
        active_phrase = phrase or self.posture_phrase
        tasks = [
            generate_proposal(
                self.provider, config, context, 1, active_phrase,
                evolution_context=None,
            )
            for config in active
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        proposals: list[Proposal] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Generator failed: {}", r)
            else:
                proposals.append(r)
        return proposals

    @staticmethod
    def _empty_verdict() -> Verdict:
        empty_proposal = Proposal(
            role=GeneratorRole.PRAGMATICO, content="", round_number=1,
        )
        scored = ScoredProposal(proposal=empty_proposal, scores=(), final_score=0.0)
        return Verdict(
            winner=scored,
            accepted=True,
            threshold=0.5,
            all_proposals=(scored,),
            rounds_used=1,
            under_doubt=True,
            convergence_reason=ConvergenceReason.MAX_ROUNDS,
        )
