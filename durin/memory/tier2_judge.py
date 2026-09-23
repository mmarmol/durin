"""Tier-2 merge judge: a bounded sub-agent that investigates a borderline pair
with the read-entity / lineage / source-session tools and returns the same
verdict envelope as the cheap judge — including the proposed resolution
(survivor, clearer keys, alias ownership, the edge between related pages).

``user_kept_separate=True`` is the re-review of a pair the user already
ruled out as a duplicate: the agent is told so, may not answer ``same``, and
only looks for what would make the two pages clearly distinct."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from durin.memory.absorb_judge import JudgeError, JudgeResult, _parse_response

AgentRunner = None  # late-bound; patched in tests

_ENVELOPE = (
    "===VERDICT===\nsame|different|related|unclear\n===CONFIDENCE===\n0-100\n"
    "===REASONING===\n<2-3 sentences; justify each proposed operation>\n"
    "===RESOLUTION===\n"
    '{{"survivor": "<ref>", "renames": {{"<ref>": {{"slug": "<new-slug>", "name": "<new name>"}}}}, '
    '"alias_moves": [{{"alias": "<alias>", "keep_on": "<ref>|both|none"}}], '
    '"relation": {{"from": "<ref>", "type": "<snake_case label>", "to": "<ref>"}}}}\n'
    "(omit keys that do not apply; {{}} when you propose nothing)\n"
    "===END==="
)

_GUIDE = (
    "Verdicts: same = one real-world entity; related = two distinct entities "
    "where one is a part, version, edition, instance or specialization of the "
    "other (merging them would lose that distinction); different = unrelated "
    "homonyms or distinct things; unclear = not enough evidence.\n"
    "Resolution (only what the evidence supports): survivor (same only) = the "
    "ref with the clearest, most canonical key; renames = a clearer slug "
    "(lowercase, digits, '-') or name when a key is cryptic or ambiguous — "
    "never change the type, and on same only the survivor may be renamed; "
    "alias_moves = an alias that belongs to only ONE of them (keep_on that "
    "ref) or is junk such as OCR noise (keep_on none) — legitimate homonyms "
    "(a first name two people share) stay on both; relation (related only) "
    "points from the more specific page to the more general one.\n"
)

_TASK = (
    "Decide what to do with these two colliding memory entities.\n"
    "Entity A: {a}\nEntity B: {b}\n\n"
    "Investigate with your tools: read each entity in full (memory_read_entity), "
    "their git lineage (memory_entity_lineage), and the source conversations "
    "(memory_source_session). Weigh consistent facts and shared specifics; be "
    "wary of homonyms.\n{extra}" + _GUIDE + "Then answer ONLY in this envelope:\n" + _ENVELOPE
)

_SEPARATED_NOTE = (
    "The user already reviewed this pair and decided it is NOT a duplicate: "
    "do not answer same. Look for what would make the two clearly distinct — a "
    "contested alias that belongs to only one of them, a cryptic key, or a "
    "structural relation between them — and answer keep-as-is (different with "
    "an empty resolution) when nothing needs to change.\n"
)

# The reserved final-answer step. Live, an investigation that needed more than
# `max_iterations` tool rounds ended with the runner's max-iterations text —
# no envelope, so every such escalation failed and the pair was re-judged on
# every run. This brief hands the model its own investigation notes and asks
# for the verdict with no tools on offer, so the budget always ends in an answer.
_FINAL_BRIEF = (
    "Your investigation budget for this pair is spent. Decide NOW from the notes "
    "below — no more tools are available.\n{extra}" + _GUIDE
    + "Answer ONLY in this envelope:\n" + _ENVELOPE + "\n\n"
    "Entity A: {a}\nEntity B: {b}\n\n"
    "Investigation notes (→ a tool call, ← what it returned):\n{notes}"
)
# Per tool result and overall, so a wide investigation still fits one call.
_NOTE_RESULT_CHARS = 1500
_NOTES_MAX_CHARS = 24_000


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text"
        )
    return "" if content is None else str(content)


def _investigation_notes(messages: list, stop_reason: str) -> str:
    """The transcript of the investigation as plain notes: the tool calls the
    agent made and what came back, plus any text it wrote — minus the task
    message and, when the run hit its ceiling, the runner's own stop text."""
    lines: list[str] = []
    last = len(messages) - 1
    for i, m in enumerate(messages):
        if i == 0 and m.get("role") == "user":
            continue
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or tc
                name = fn.get("name") or tc.get("name")
                args = fn.get("arguments") if "function" in tc else tc.get("arguments")
                lines.append(f"→ {name}({_text_of(args)[:300]})")
            text = _text_of(m.get("content")).strip()
            if text and not (i == last and stop_reason == "max_iterations"):
                lines.append(f"note: {text[:_NOTE_RESULT_CHARS]}")
        elif role == "tool":
            lines.append(f"← {_text_of(m.get('content'))[:_NOTE_RESULT_CHARS]}")
    out = "\n".join(lines)
    if len(out) > _NOTES_MAX_CHARS:
        out = out[:_NOTES_MAX_CHARS] + "\n…(notes truncated)"
    return out or "(the investigation returned nothing)"


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
    user_kept_separate: bool = False,
) -> JudgeResult:
    global AgentRunner
    if AgentRunner is None:
        from durin.agent.runner import AgentRunner as _AR
        AgentRunner = _AR
    from durin.agent.runner import AgentRunSpec

    if provider is None or not model:
        provider, model = _resolve_provider_model()

    extra = _SEPARATED_NOTE if user_kept_separate else ""
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": _TASK.format(a=ref_a, b=ref_b, extra=extra)}],
        tools=_build_tools(Path(workspace)),
        model=model,
        max_iterations=max_iterations,
        max_tool_result_chars=8000,
        fail_on_tool_error=False,
        workspace=Path(workspace),
    )
    result = await AgentRunner(provider).run(spec)
    try:
        return _parse_response(result.final_content or "")
    except JudgeError:
        pass
    # One more call, no tools: the agent must answer from what it has read.
    from durin.agent.tools.registry import ToolRegistry
    brief = _FINAL_BRIEF.format(
        a=ref_a, b=ref_b, extra=extra,
        notes=_investigation_notes(
            list(getattr(result, "messages", None) or []),
            str(getattr(result, "stop_reason", "") or ""),
        ),
    )
    final_spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": brief}],
        tools=ToolRegistry(),
        model=model,
        max_iterations=1,
        max_tool_result_chars=8000,
        fail_on_tool_error=False,
        workspace=Path(workspace),
    )
    final = await AgentRunner(provider).run(final_spec)
    return _parse_response(final.final_content or "")


def escalate_judge(
    workspace: Any,
    ref_a: str,
    ref_b: str,
    *,
    provider: Any = None,
    model: str | None = None,
    max_iterations: int = 6,
    user_kept_separate: bool = False,
) -> JudgeResult:
    """Escalate a borderline pair to a bounded sub-agent for investigation.

    Runs synchronously via asyncio.run; safe to call from a worker thread
    (cron uses asyncio.to_thread which starts a fresh thread with no running
    event loop).
    """
    from durin.telemetry.logger import bind_call_purpose, reset_call_purpose

    # The judge runs inside the dream's telemetry binding; its own calls are
    # billed as ``judge`` so the escalation's cost is separable from the pass.
    token = bind_call_purpose("judge")
    try:
        return asyncio.run(
            _escalate_async(
                workspace,
                ref_a,
                ref_b,
                provider=provider,
                model=model,
                max_iterations=max_iterations,
                user_kept_separate=user_kept_separate,
            )
        )
    finally:
        reset_call_purpose(token)
