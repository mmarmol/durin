"""Engine — runs the evolutionary multi-round deliberation cycle.

Round 1: Divergent generation (seeds from each perspective).
Round 2+: Evolutionary refinement (generators refine based on previous round's winner).
Convergence: threshold acceptance, score plateau, or hard cap.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from loguru import logger

from durin.deliberation.director import decide
from durin.deliberation.evaluator import Evaluator
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

_PLATEAU_THRESHOLD = 0.05
_CROSSOVER_GAP = 0.10

_CROSSOVER_SYSTEM = (
    "Sos un integrador. Combiná los puntos fuertes de las dos propuestas "
    "en una síntesis que sea mejor que ambas. Respondé en 2-3 oraciones."
)


def _extra_rounds(profundidad: float) -> int:
    """Additional rounds granted by high profundidad."""
    if profundidad >= 0.8:
        return 2
    if profundidad >= 0.6:
        return 1
    return 0


@dataclass(slots=True)
class DeliberationEngine:
    provider: LLMProvider
    generators: list[GeneratorConfig]
    evaluators: list[Evaluator]
    max_rounds: int = 3
    posture_phrase: str = ""

    async def deliberate(self, context: DeliberationContext) -> Verdict:
        cautela = context.posture_snapshot.get("cautela", 0.5)
        profundidad = context.posture_snapshot.get("profundidad", 0.5)

        active_generators = modulate_generators(self.generators, context.posture_snapshot)
        active_phrase = phrase_from_snapshot(context.posture_snapshot) if context.posture_snapshot else self.posture_phrase
        effective_max_rounds = min(self.max_rounds + _extra_rounds(profundidad), 5)

        all_proposals: list[Proposal] = []
        all_evaluations: dict[str, list[EvaluationScore]] = {}
        previous_round: RoundResult | None = None
        best_score_history: list[float] = []
        verdict: Verdict | None = None

        for round_num in range(1, effective_max_rounds + 1):
            proposals = await self._generate_round(
                context, round_num,
                generators=active_generators,
                phrase=active_phrase,
                previous_round=previous_round,
            )
            if not proposals:
                logger.warning("All generators failed in round {}", round_num)
                if not all_proposals:
                    return self._empty_verdict(profundidad, round_num)
                break

            all_proposals.extend(proposals)
            round_evals = await self._evaluate_proposals(proposals, context)
            all_evaluations.update(round_evals)

            verdict = decide(
                proposals=all_proposals,
                evaluations=all_evaluations,
                cautela=cautela,
                profundidad=profundidad,
                round_number=round_num,
                max_rounds=effective_max_rounds,
            )

            scored_this_round = self._score_proposals_from_verdict(proposals, verdict)

            hybrid = await self._maybe_crossover(
                scored_this_round, context, round_num, active_phrase,
            ) if round_num > 1 else None
            if hybrid is not None:
                all_proposals.append(hybrid.proposal)
                hybrid_key = f"{hybrid.proposal.role}:{hybrid.proposal.round_number}"
                all_evaluations[hybrid_key] = list(hybrid.scores)
                scored_this_round = (*scored_this_round, hybrid)
                verdict = decide(
                    proposals=all_proposals,
                    evaluations=all_evaluations,
                    cautela=cautela,
                    profundidad=profundidad,
                    round_number=round_num,
                    max_rounds=effective_max_rounds,
                )

            previous_round = RoundResult(
                proposals=scored_this_round,
                winner=verdict.winner,
                round_number=round_num,
            )

            best_score_history.append(verdict.winner.final_score)

            if verdict.accepted:
                reason = ConvergenceReason.MAX_ROUNDS if verdict.under_doubt else ConvergenceReason.THRESHOLD
                return replace(verdict, convergence_reason=reason)

            if len(best_score_history) >= 2:
                improvement = best_score_history[-1] - best_score_history[-2]
                if improvement < _PLATEAU_THRESHOLD:
                    below_threshold = verdict.winner.final_score < verdict.threshold
                    return replace(
                        verdict,
                        accepted=True,
                        under_doubt=below_threshold,
                        convergence_reason=ConvergenceReason.PLATEAU,
                    )

            logger.debug(
                "Round {} — best={:.2f}, threshold={:.2f}. Retrying.",
                round_num, verdict.winner.final_score, verdict.threshold,
            )

        assert verdict is not None
        return replace(verdict, convergence_reason=ConvergenceReason.MAX_ROUNDS)

    async def _generate_round(
        self,
        context: DeliberationContext,
        round_number: int,
        *,
        generators: list[GeneratorConfig] | None = None,
        phrase: str = "",
        previous_round: RoundResult | None = None,
    ) -> list[Proposal]:
        active = generators if generators is not None else self.generators
        active_phrase = phrase or self.posture_phrase
        tasks = [
            generate_proposal(
                self.provider, config, context, round_number, active_phrase,
                evolution_context=previous_round,
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

    async def _evaluate_proposals(
        self,
        proposals: list[Proposal],
        context: DeliberationContext,
    ) -> dict[str, list[EvaluationScore]]:
        evaluations: dict[str, list[EvaluationScore]] = {}
        tasks = []
        keys: list[tuple[str, str]] = []

        for proposal in proposals:
            key = f"{proposal.role}:{proposal.round_number}"
            for evaluator in self.evaluators:
                tasks.append(evaluator.evaluate(proposal, context))
                keys.append((key, evaluator.name))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (key, eval_name), result in zip(keys, results):
            if isinstance(result, Exception):
                logger.warning("Evaluator {} failed for {}: {}", eval_name, key, result)
                result = EvaluationScore(evaluator_name=eval_name, score=0.5)
            evaluations.setdefault(key, []).append(result)

        return evaluations

    async def _maybe_crossover(
        self,
        scored: tuple[ScoredProposal, ...] | list[ScoredProposal],
        context: DeliberationContext,
        round_number: int,
        phrase: str,
    ) -> ScoredProposal | None:
        if len(scored) < 2:
            return None
        top2 = sorted(scored, key=lambda s: s.final_score, reverse=True)[:2]
        gap = top2[0].final_score - top2[1].final_score
        if gap >= _CROSSOVER_GAP:
            return None

        hybrid_proposal = await self._generate_crossover_proposal(
            top2[0], top2[1], context, round_number, phrase,
        )
        hybrid_evals = await self._evaluate_proposals([hybrid_proposal], context)
        key = f"{hybrid_proposal.role}:{hybrid_proposal.round_number}"
        scores = tuple(hybrid_evals.get(key, []))

        from durin.deliberation.scoring import compute_final_score, compute_threshold

        cautela = context.posture_snapshot.get("cautela", 0.5)
        avance = next((s.score for s in scores if s.evaluator_name == "avance"), 0.5)
        reversibilidad = next((s.score for s in scores if s.evaluator_name == "reversibilidad"), 0.5)
        final_score = compute_final_score(avance, reversibilidad, cautela)

        return ScoredProposal(
            proposal=hybrid_proposal, scores=scores, final_score=final_score,
        )

    async def _generate_crossover_proposal(
        self,
        first: ScoredProposal,
        second: ScoredProposal,
        context: DeliberationContext,
        round_number: int,
        phrase: str,
    ) -> Proposal:
        user_content = (
            f"Objetivo: {context.goal_summary}\n\n"
            f"Propuesta A ({first.proposal.role}, score {first.final_score:.0%}):\n"
            f'"{first.proposal.content[:200]}"\n\n'
            f"Propuesta B ({second.proposal.role}, score {second.final_score:.0%}):\n"
            f'"{second.proposal.content[:200]}"'
        )
        system_parts = [_CROSSOVER_SYSTEM]
        if phrase:
            system_parts.append(phrase)

        config = self.generators[0] if self.generators else GeneratorConfig(
            role=GeneratorRole.HIBRIDO, model="default", temperature=0.5,
        )

        response = await self.provider.chat(
            messages=[
                {"role": "system", "content": "\n\n".join(system_parts)},
                {"role": "user", "content": user_content},
            ],
            tools=None,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=0.5,
        )

        return Proposal(
            role=GeneratorRole.HIBRIDO,
            content=response.content or "",
            round_number=round_number,
        )

    @staticmethod
    def _score_proposals_from_verdict(
        proposals: list[Proposal], verdict: Verdict,
    ) -> tuple[ScoredProposal, ...]:
        scored = []
        for sp in verdict.all_proposals:
            if sp.proposal in proposals:
                scored.append(sp)
        return tuple(scored)

    @staticmethod
    def _empty_verdict(profundidad: float, round_number: int) -> Verdict:
        from durin.deliberation.scoring import compute_threshold

        empty_proposal = Proposal(
            role=GeneratorRole.PRAGMATICO, content="", round_number=round_number,
        )
        scored = ScoredProposal(proposal=empty_proposal, scores=(), final_score=0.0)
        return Verdict(
            winner=scored,
            accepted=True,
            threshold=compute_threshold(profundidad),
            all_proposals=(scored,),
            rounds_used=round_number,
            under_doubt=True,
            convergence_reason=ConvergenceReason.MAX_ROUNDS,
        )
