"""Tier-2 merge judge: a bounded sub-agent that investigates a borderline pair
with the read-entity / lineage / source-session tools and returns the same
verdict envelope as the cheap judge."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from durin.memory.absorb_judge import JudgeResult, _parse_response

AgentRunner = None  # late-bound; patched in tests

_TASK = (
    "Decide whether these two memory entities are the SAME real-world entity.\n"
    "Entity A: {a}\nEntity B: {b}\n\n"
    "Investigate with your tools: read each entity in full (memory_read_entity), "
    "their git lineage (memory_entity_lineage), and the source conversations "
    "(memory_source_session). Weigh consistent facts and shared specifics; be "
    "wary of homonyms. Then answer ONLY in this envelope:\n"
    "===VERDICT===\nsame|different|unclear\n===CONFIDENCE===\n0-100\n"
    "===REASONING===\n<2-3 sentences>\n===END==="
)


def _resolve_provider_model() -> tuple[Any, str]:
    from durin.config.loader import load_config
    from durin.memory.model_resolve import resolve_aux_preset
    from durin.providers.factory import make_provider
    cfg = load_config()
    # Provider and model must come from the SAME resolved preset — building the
    # provider from the default preset while taking the judge preset's model
    # sent the judge's model name to the wrong endpoint.
    preset = resolve_aux_preset(cfg, purpose="judge")
    return make_provider(cfg, preset=preset), preset.model


def _build_tools(workspace: Path) -> Any:
    from durin.agent.tools.registry import ToolRegistry
    from durin.agent.tools.memory_lineage_tools import (
        MemoryEntityLineageTool,
        MemoryReadEntityTool,
        MemorySourceSessionTool,
    )
    from durin.agent.tools.memory_search import MemorySearchTool

    t = ToolRegistry()
    t.register(MemoryReadEntityTool(workspace))
    t.register(MemoryEntityLineageTool(workspace))
    t.register(MemorySourceSessionTool(workspace))
    try:
        t.register(MemorySearchTool(workspace=workspace))
    except Exception:  # noqa: BLE001 — search optional
        pass
    return t


async def _escalate_async(
    workspace: Any,
    ref_a: str,
    ref_b: str,
    *,
    provider: Any,
    model: str | None,
    max_iterations: int,
) -> JudgeResult:
    global AgentRunner
    if AgentRunner is None:
        from durin.agent.runner import AgentRunner as _AR
        AgentRunner = _AR
    from durin.agent.runner import AgentRunSpec

    if provider is None or not model:
        provider, model = _resolve_provider_model()

    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": _TASK.format(a=ref_a, b=ref_b)}],
        tools=_build_tools(Path(workspace)),
        model=model,
        max_iterations=max_iterations,
        max_tool_result_chars=8000,
        fail_on_tool_error=False,
        workspace=Path(workspace),
    )
    result = await AgentRunner(provider).run(spec)
    return _parse_response(result.final_content or "")


def escalate_judge(
    workspace: Any,
    ref_a: str,
    ref_b: str,
    *,
    provider: Any = None,
    model: str | None = None,
    max_iterations: int = 6,
) -> JudgeResult:
    """Escalate a borderline pair to a bounded sub-agent for investigation.

    Runs synchronously via asyncio.run; safe to call from a worker thread
    (cron uses asyncio.to_thread which starts a fresh thread with no running
    event loop).
    """
    return asyncio.run(
        _escalate_async(
            workspace,
            ref_a,
            ref_b,
            provider=provider,
            model=model,
            max_iterations=max_iterations,
        )
    )
