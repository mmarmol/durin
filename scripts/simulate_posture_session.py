"""Simulate a realistic multi-step agent session to observe posture + deliberation behavior.

Scenario: "Deploy microservice to production with database migration"
- A risky, multi-step task that should trigger cautela up
- Some steps fail, some succeed
- User corrects the agent mid-way
- A protocol is detected in the system prompt
- The agent eventually stabilizes

This prints the full trajectory of the posture vector and deliberation decisions.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from durin.agent.hook import AgentHookContext
from durin.deliberation.engine import DeliberationEngine, _extra_rounds
from durin.deliberation.evaluator import Evaluator
from durin.deliberation.hook import DeliberationHook
from durin.deliberation.modulator import modulate_generators, phrase_from_snapshot
from durin.deliberation.types import (
    DeliberationContext,
    EvaluationScore,
    GeneratorRole,
    Proposal,
    ScoredProposal,
    TriggerReason,
    Verdict,
)
from durin.posture.goal_bias import compute_goal_bias
from durin.posture.hook import PostureHook
from durin.posture.stimulus import StimulusTable
from durin.posture.vector import AxisName, AxisState, PostureVector
from durin.providers.base import LLMProvider, LLMResponse, ToolCallRequest


# --- Mock provider for simulation ---

class SimulatedProvider(LLMProvider):
    """Returns canned proposals based on role."""

    _proposals = {
        GeneratorRole.PRAGMATICO: [
            "Ejecutar migration con --dry-run primero, luego aplicar si OK.",
            "Aplicar migration directa, rollback automático si falla.",
            "Deploy con blue-green, cutover manual después de validar.",
        ],
        GeneratorRole.EXPLORADOR: [
            "Usar shadow traffic para validar antes del deploy real.",
            "Probar migration en réplica read-only primero.",
            "Deploy canary al 5%, monitorear 30min, luego full.",
        ],
        GeneratorRole.CRITICO: [
            "Crear snapshot de la DB antes. Si algo falla, restore inmediato.",
            "Bloquear deploys hasta tener rollback testeado manualmente.",
            "Separar migration del deploy: primero DB, validar, luego código.",
        ],
    }
    _call_count = 0

    async def chat(self, **kwargs) -> LLMResponse:
        self._call_count += 1
        messages = kwargs.get("messages", [])
        # Determine role from system message
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "pragmático" in system:
            role = GeneratorRole.PRAGMATICO
        elif "explorador" in system:
            role = GeneratorRole.EXPLORADOR
        else:
            role = GeneratorRole.CRITICO

        proposals = self._proposals[role]
        content = proposals[self._call_count % len(proposals)]
        return LLMResponse(content=content, tool_calls=[], finish_reason="stop", usage={})


class SimulatedEvaluator(Evaluator):
    """Returns semi-realistic scores based on role and scenario phase."""

    def __init__(self, name: str, base_scores: dict[GeneratorRole, float]):
        self._name = name
        self._base_scores = base_scores

    @property
    def name(self) -> str:
        return self._name

    async def evaluate(self, proposal: Proposal, context: DeliberationContext) -> EvaluationScore:
        base = self._base_scores.get(proposal.role, 0.5)
        # Add some variance per round
        variance = (proposal.round_number - 1) * 0.05
        return EvaluationScore(evaluator_name=self._name, score=min(1.0, base + variance))


# --- Simulation steps ---

@dataclass
class SimStep:
    description: str
    iteration: int
    tool_calls: list[str]
    tool_results: list[dict]
    error: str | None = None
    final_content: str | None = None
    injected_messages_count: int = 0
    system_prompt: str = "You are a deployment agent."


SCENARIO = [
    SimStep(
        description="Iteration 0: Agent receives goal, starts planning",
        iteration=0,
        tool_calls=[],
        tool_results=[],
        system_prompt="You are a deployment agent.\n\n## Steps\n\n1. Validate config\n2. Run migration\n3. Deploy",
    ),
    SimStep(
        description="Iteration 1: Read config file — success",
        iteration=1,
        tool_calls=["read_file"],
        tool_results=[{"content": "config loaded"}],
    ),
    SimStep(
        description="Iteration 2: Validate schema — success",
        iteration=2,
        tool_calls=["read_file"],
        tool_results=[{"content": "schema valid"}],
    ),
    SimStep(
        description="Iteration 3: Run migration dry-run — success",
        iteration=3,
        tool_calls=["exec"],
        tool_results=[{"content": "dry-run OK, 3 tables affected"}],
    ),
    SimStep(
        description="Iteration 4: Apply migration — FAILS",
        iteration=4,
        tool_calls=["exec"],
        tool_results=[{"error": "connection timeout to primary DB"}],
        error="Migration failed: connection timeout",
    ),
    SimStep(
        description="Iteration 5: Retry migration — FAILS again",
        iteration=5,
        tool_calls=["exec"],
        tool_results=[{"error": "lock timeout exceeded"}],
        error="Migration failed: lock timeout",
    ),
    SimStep(
        description="Iteration 6: Agent confused, no action",
        iteration=6,
        tool_calls=[],
        tool_results=[],
        final_content=None,
    ),
    SimStep(
        description="Iteration 7: User corrects — 'usa la réplica primero'",
        iteration=7,
        tool_calls=["exec"],
        tool_results=[{"content": "replica migration OK"}],
        injected_messages_count=1,
    ),
    SimStep(
        description="Iteration 8: Success on replica — agent tries primary again",
        iteration=8,
        tool_calls=["exec"],
        tool_results=[{"content": "primary migration applied"}],
    ),
    SimStep(
        description="Iteration 9: Deploy service — critical action",
        iteration=9,
        tool_calls=["deploy"],
        tool_results=[{"content": "deployed v2.1.0"}],
    ),
    SimStep(
        description="Iteration 10: Health check — success",
        iteration=10,
        tool_calls=["read_file"],
        tool_results=[{"content": "health OK, 0 errors"}],
    ),
    SimStep(
        description="Iteration 11: Final validation — success",
        iteration=11,
        tool_calls=["read_file"],
        tool_results=[{"content": "all endpoints responding"}],
    ),
    SimStep(
        description="Iteration 12: Cleanup temp resources — success",
        iteration=12,
        tool_calls=["exec"],
        tool_results=[{"content": "cleaned up"}],
    ),
]


def print_vector(vector: PostureVector, label: str = "") -> None:
    snapshot = vector.snapshot()
    parts = []
    for name in AxisName:
        v = snapshot[name]
        bar = "█" * int(v * 20) + "░" * (20 - int(v * 20))
        parts.append(f"  {name.value:13s} {bar} {v:.3f}")
    print(f"\n{'─' * 50}")
    if label:
        print(f"  {label}")
    print("\n".join(parts))


def print_modulation(snapshot: dict) -> None:
    from durin.deliberation.generator import GeneratorConfig
    gens = [
        GeneratorConfig(role=GeneratorRole.PRAGMATICO, model="sim", temperature=0.3, prompt_template="pragmatico"),
        GeneratorConfig(role=GeneratorRole.EXPLORADOR, model="sim", temperature=0.8, prompt_template="explorador"),
        GeneratorConfig(role=GeneratorRole.CRITICO, model="sim", temperature=0.5, prompt_template="critico"),
    ]
    modulated = modulate_generators(gens, snapshot)
    profundidad = snapshot.get("profundidad", 0.5)
    cautela = snapshot.get("cautela", 0.5)

    print(f"\n  Modulación estructural:")
    print(f"    Generators: {len(modulated)} ({', '.join(g.role + f'@{g.temperature:.2f}' for g in modulated)})")
    print(f"    Extra rounds: +{_extra_rounds(profundidad)} (effective max: {3 + _extra_rounds(profundidad)})")
    drift_th = 0.15 - 0.05 * (cautela - 0.5)
    print(f"    Drift threshold: {drift_th:.3f}")
    phrase = phrase_from_snapshot(snapshot)
    if phrase:
        print(f"    Phrase: {phrase}")


async def run_simulation():
    print("=" * 60)
    print("SIMULACIÓN: Deploy microservice con DB migration")
    print("=" * 60)

    # Setup
    vector = PostureVector.default()
    hook = PostureHook(vector=vector)

    # Show initial state
    print_vector(hook.current_vector, "ESTADO INICIAL (default)")

    # Show goal bias
    goal = "deploy microservice a producción con database migration"
    bias = compute_goal_bias(goal)
    print(f"\n  Goal: '{goal}'")
    print(f"  Goal bias: {', '.join(f'{k.value} +{v:.2f}' for k, v in bias.items()) or 'ninguno'}")

    # Run through scenario
    for step in SCENARIO:
        print(f"\n{'═' * 60}")
        print(f"  STEP {step.iteration}: {step.description}")
        print(f"{'═' * 60}")

        tool_calls = [ToolCallRequest(id=f"t{i}", name=name, arguments="{}") for i, name in enumerate(step.tool_calls)]

        ctx = AgentHookContext(
            iteration=step.iteration,
            messages=[
                {"role": "system", "content": step.system_prompt},
                {"role": "user", "content": goal},
            ],
            tool_calls=tool_calls,
            tool_results=step.tool_results,
            error=step.error,
            final_content=step.final_content,
            injected_messages_count=step.injected_messages_count,
        )

        # before_iteration (for iteration 0 goal bias + emit)
        await hook.before_iteration(ctx)

        # Capture state before to detect changes
        snapshot_before = hook.current_vector.snapshot()

        # after_iteration (event detection + vector update) — single call, like real loop
        await hook.after_iteration(ctx)

        # Infer which events fired by comparing snapshots
        snapshot_after = hook.current_vector.snapshot()
        changed_axes = {
            k: round(snapshot_after[k] - snapshot_before[k], 4)
            for k in snapshot_after
            if abs(snapshot_after[k] - snapshot_before[k]) > 0.001
        }

        # Report events from axis changes
        event_hints = []
        if step.error or any(r.get("error") for r in step.tool_results):
            event_hints.append("step_failed")
        elif step.tool_calls:
            event_hints.append("step_succeeded")
        if step.injected_messages_count > 0:
            event_hints.append("user_corrected")
        if step.iteration > 0 and not step.tool_calls and not step.final_content and not step.error:
            event_hints.append("goal_ambiguous")
        if any(t in ("exec", "shell", "deploy") for t in step.tool_calls):
            event_hints.append("critical_action")

        print(f"  Events (inferred): {', '.join(event_hints) or '(none)'}")
        if changed_axes:
            print(f"  Axis changes: {', '.join(f'{k.value} {v:+.4f}' for k, v in changed_axes.items())}")

        if step.error:
            print(f"  ⚠ Error: {step.error}")

        print_vector(hook.current_vector, f"After step {step.iteration}")

        # Show modulation at key moments
        if step.iteration in (0, 4, 6, 7, 9, 12):
            snapshot = hook.current_vector.snapshot()
            print_modulation({k.value if hasattr(k, 'value') else k: v for k, v in snapshot.items()})

    # Final summary
    print(f"\n{'═' * 60}")
    print("  RESUMEN FINAL")
    print(f"{'═' * 60}")
    print_vector(hook.current_vector, "ESTADO FINAL")

    initial = PostureVector.default().snapshot()
    final = hook.current_vector.snapshot()
    print(f"\n  Cambios netos vs default:")
    for name in AxisName:
        delta = final[name] - initial[name]
        if abs(delta) > 0.001:
            direction = "↑" if delta > 0 else "↓"
            print(f"    {name.value:13s}: {direction} {abs(delta):.3f}")

    final_dict = {k.value: v for k, v in final.items()}
    print_modulation(final_dict)


if __name__ == "__main__":
    asyncio.run(run_simulation())
