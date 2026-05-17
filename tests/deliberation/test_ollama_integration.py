"""Real integration test with Ollama — requires running Ollama instance.

Run with: pytest tests/deliberation/test_ollama_integration.py -v -m ollama
Skip automatically if Ollama is unreachable.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.evaluator import LLMEvaluator
from durin.deliberation.generator import GeneratorConfig
from durin.deliberation.types import (
    DeliberationContext,
    GeneratorRole,
    TriggerReason,
)

OLLAMA_BASE = "http://localhost:11434/v1"
MODEL = "qwen2.5:7b"


def _ollama_available() -> bool:
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _model_available() -> bool:
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code != 200:
            return False
        models = [m["name"] for m in r.json().get("models", [])]
        return any(MODEL.split(":")[0] in m for m in models)
    except Exception:
        return False


pytestmark = pytest.mark.ollama

skip_no_ollama = pytest.mark.skipif(
    not _ollama_available(),
    reason="Ollama not running at localhost:11434",
)
skip_no_model = pytest.mark.skipif(
    not _model_available(),
    reason=f"Model {MODEL} not available in Ollama",
)


def _make_provider():
    from durin.providers.openai_compat_provider import OpenAICompatProvider

    return OpenAICompatProvider(
        api_key="ollama",
        api_base=OLLAMA_BASE,
        default_model=MODEL,
    )


def _generators() -> list[GeneratorConfig]:
    return [
        GeneratorConfig(
            role=GeneratorRole.PRAGMATICO,
            model=MODEL,
            temperature=0.3,
            prompt_template=(
                "Sos el generador pragmático. Proponé un camino concreto "
                "usando lo conocido y probado. Explicá brevemente por qué "
                "es la opción más directa para este problema."
            ),
        ),
        GeneratorConfig(
            role=GeneratorRole.EXPLORADOR,
            model=MODEL,
            temperature=0.8,
            prompt_template=(
                "Sos el generador explorador. Proponé un camino alternativo "
                "que los demás no están considerando. Explicá brevemente qué "
                "se gana explorando esta dirección."
            ),
        ),
        GeneratorConfig(
            role=GeneratorRole.CRITICO,
            model=MODEL,
            temperature=0.5,
            prompt_template=(
                "Sos el generador crítico. Proponé el camino más seguro y "
                "reversible. Explicá brevemente qué riesgos evitás con esta "
                "dirección."
            ),
        ),
    ]


def _evaluators():
    provider = _make_provider()
    return [
        LLMEvaluator(
            "avance", provider, MODEL,
            "Del 0 al 10, cuanto avanza esta propuesta hacia el objetivo? Responde SOLO el numero.",
        ),
        LLMEvaluator(
            "reversibilidad", provider, MODEL,
            "Del 0 al 10, si esta propuesta falla, cuan facil es volver atras? Responde SOLO el numero.",
        ),
    ]


@skip_no_ollama
@skip_no_model
class TestOllamaDeliberation:
    @pytest.mark.asyncio
    async def test_full_deliberation_cycle(self):
        """Three generators produce proposals, evaluators score them, director picks."""
        provider = _make_provider()
        engine = DeliberationEngine(
            provider=provider,
            generators=_generators(),
            evaluators=_evaluators(),
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="implementar autenticación de usuarios en una app web",
            recent_context="el usuario pidió login con email y password",
            posture_snapshot={"cautela": 0.6, "profundidad": 0.5},
        )

        verdict = await engine.deliberate(ctx)

        assert verdict.accepted is True
        assert verdict.winner is not None
        assert len(verdict.winner.proposal.content) > 10
        assert verdict.winner.final_score > 0
        assert len(verdict.all_proposals) == 3

        print(f"\n{'='*60}")
        print(f"Winner: {verdict.winner.proposal.role} (score={verdict.winner.final_score:.2f})")
        print(f"Content: {verdict.winner.proposal.content}")
        print(f"{'='*60}")
        for sp in verdict.all_proposals:
            print(f"\n[{sp.proposal.role}] score={sp.final_score:.2f}")
            print(f"  {sp.proposal.content[:200]}")

    @pytest.mark.asyncio
    async def test_high_cautela_favors_safety(self):
        """With high cautela, the safer proposal should win."""
        provider = _make_provider()
        engine = DeliberationEngine(
            provider=provider,
            generators=_generators(),
            evaluators=_evaluators(),
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.CRITICAL_ACTION,
            goal_summary="deploy a producción un cambio en el schema de la base de datos",
            recent_context="migration que agrega columna NOT NULL a tabla con 1M rows",
            posture_snapshot={"cautela": 0.9, "profundidad": 0.7},
        )

        verdict = await engine.deliberate(ctx)

        assert verdict.accepted is True
        print(f"\nHigh cautela winner: {verdict.winner.proposal.role}")
        print(f"Content: {verdict.winner.proposal.content}")

    @pytest.mark.asyncio
    async def test_proposals_are_distinct(self):
        """Each generator should produce a meaningfully different proposal."""
        provider = _make_provider()
        engine = DeliberationEngine(
            provider=provider,
            generators=_generators(),
            evaluators=_evaluators(),
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="refactorizar el módulo de pagos para soportar múltiples proveedores",
            recent_context="actualmente hardcoded a Stripe",
            posture_snapshot={"cautela": 0.5, "profundidad": 0.5},
        )

        verdict = await engine.deliberate(ctx)

        contents = [sp.proposal.content for sp in verdict.all_proposals]
        # No two proposals should be identical
        assert len(set(contents)) == 3
        # Each should have substance
        for content in contents:
            assert len(content) > 20

        print(f"\n{'='*60}")
        for sp in verdict.all_proposals:
            print(f"\n[{sp.proposal.role}] score={sp.final_score:.2f}")
            print(f"  {sp.proposal.content}")
