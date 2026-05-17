"""Integration test using local llama-cpp-python — no external service needed.

Run with: pytest tests/deliberation/test_local_integration.py -v -m local_llm
Skips automatically if llama-cpp-python is not installed or model not cached.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.local_llm


def _llama_cpp_available() -> bool:
    try:
        import llama_cpp
        return True
    except ImportError:
        return False


def _model_cached() -> bool:
    from durin.providers.local_llama_provider import MODELS, _DEFAULT_CACHE_DIR
    spec = MODELS["qwen3b"]
    model_dir = _DEFAULT_CACHE_DIR / spec.repo_id.replace("/", "--")
    return (model_dir / spec.filename).exists()


skip_no_llama = pytest.mark.skipif(
    not _llama_cpp_available(),
    reason="llama-cpp-python not installed (pip install 'durin[local]')",
)


@skip_no_llama
class TestLocalProvider:
    @pytest.mark.asyncio
    async def test_provider_chat_returns_response(self):
        from durin.providers.local_llama_provider import LocalLlamaProvider

        if not _model_cached():
            pytest.skip("Model not cached — run once with network to download")

        provider = LocalLlamaProvider()
        response = await provider.chat(
            messages=[
                {"role": "system", "content": "Respondé en una oración."},
                {"role": "user", "content": "¿Qué es OAuth2?"},
            ],
            max_tokens=100,
            temperature=0.3,
        )
        assert response.content
        assert len(response.content) > 5
        assert response.usage["completion_tokens"] > 0

    @pytest.mark.asyncio
    async def test_full_deliberation_with_local_provider(self):
        from durin.deliberation.engine import DeliberationEngine
        from durin.deliberation.evaluator import LLMEvaluator
        from durin.deliberation.generator import GeneratorConfig
        from durin.deliberation.types import (
            DeliberationContext,
            GeneratorRole,
            TriggerReason,
        )
        from durin.providers.local_llama_provider import LocalLlamaProvider

        if not _model_cached():
            pytest.skip("Model not cached — run once with network to download")

        provider = LocalLlamaProvider()

        generators = [
            GeneratorConfig(
                role=GeneratorRole.PRAGMATICO,
                model="local",
                temperature=0.3,
                prompt_template=(
                    "Sos el generador pragmático. Proponé un camino concreto "
                    "usando lo conocido y probado. Explicá brevemente por qué."
                ),
            ),
            GeneratorConfig(
                role=GeneratorRole.EXPLORADOR,
                model="local",
                temperature=0.8,
                prompt_template=(
                    "Sos el generador explorador. Proponé un camino alternativo. "
                    "Explicá brevemente qué se gana explorando esta dirección."
                ),
            ),
            GeneratorConfig(
                role=GeneratorRole.CRITICO,
                model="local",
                temperature=0.5,
                prompt_template=(
                    "Sos el generador crítico. Proponé el camino más seguro. "
                    "Explicá brevemente qué riesgos evitás."
                ),
            ),
        ]

        evaluators = [
            LLMEvaluator(
                "avance", provider, "local",
                "Del 0 al 10, cuanto avanza esta propuesta hacia el objetivo? Responde SOLO el numero.",
            ),
            LLMEvaluator(
                "reversibilidad", provider, "local",
                "Del 0 al 10, si esta propuesta falla, cuan facil es volver atras? Responde SOLO el numero.",
            ),
        ]

        engine = DeliberationEngine(
            provider=provider,
            generators=generators,
            evaluators=evaluators,
            max_rounds=1,
        )

        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="implementar autenticación de usuarios",
            recent_context="app web nueva, el usuario pidió login con email",
            posture_snapshot={"cautela": 0.6, "profundidad": 0.5},
        )

        verdict = await engine.deliberate(ctx)

        assert verdict.accepted is True
        assert verdict.winner is not None
        assert len(verdict.winner.proposal.content) > 10
        assert len(verdict.all_proposals) == 3

        print(f"\n{'='*60}")
        print(f"Winner: {verdict.winner.proposal.role} (score={verdict.winner.final_score:.2f})")
        print(f"Content: {verdict.winner.proposal.content}")
        print(f"{'='*60}")
        for sp in verdict.all_proposals:
            print(f"\n[{sp.proposal.role}] score={sp.final_score:.2f}")
            print(f"  {sp.proposal.content[:200]}")
