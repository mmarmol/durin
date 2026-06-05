"""Cron entry points for the new dreams (Phase 8c/8d).

The new model has two dreams, split by CADENCE:

- **extract** (frequent, the ~2h `dream` cron): read each session's new turns
  and extract structured entity attributes (``run_extract_pass``).
- **refine** (periodic, the daily `memory_dream` cron): dedup/merge duplicate
  entities (``run_refine_pass``).

These REPLACE the legacy ``DreamRunner`` / ``DreamConsolidator`` at the cron
callsites — the legacy consolidated episodic entries into pages via JSON-Patch
+ working-tree writes (the obsolete model + the G3 race). Both new passes write
through ``memory_writer`` (plumbing + CAS).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from durin.memory.extract_runner import run_extract_for_session
from durin.memory.refine_dream import run_refine

__all__ = ["run_extract_pass", "run_refine_pass"]

LLMInvoke = Callable[..., Any]


def run_extract_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
) -> dict:
    """Run the extract dream over every session that has new turns.

    Per-session cursors make this idempotent — a session with no new turns is
    skipped. Best-effort per session: one bad session doesn't abort the pass.
    """
    import time
    t0 = time.perf_counter()
    _emit("memory.dream.start", kind="extract")
    sessions_dir = Path(workspace) / "sessions"
    out: dict[str, Any] = {"sessions": 0, "entities": 0, "errors": []}
    if sessions_dir.is_dir():
        for jsonl_path in sorted(sessions_dir.glob("*.jsonl")):
            try:
                r = run_extract_for_session(
                    workspace, jsonl_path, llm_invoke=llm_invoke, model=model)
                extracted = r.get("extracted") or []
                if extracted:
                    out["sessions"] += 1
                    out["entities"] += len(extracted)
            except Exception as exc:  # noqa: BLE001 — never abort the whole pass
                out["errors"].append({"session": jsonl_path.stem, "error": str(exc)})
    _emit("memory.dream.end", kind="extract",
          entities_consolidated=out["entities"], entities_failed=len(out["errors"]),
          sessions=out["sessions"],
          duration_ms=int((time.perf_counter() - t0) * 1000))
    return out


def run_refine_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str = "glm-5.1",
) -> dict:
    """Run the refine dream (dedup duplicate entities). The daily cron entry."""
    import time
    t0 = time.perf_counter()
    _emit("memory.dream.start", kind="refine")
    out = run_refine(workspace, llm_invoke=llm_invoke, model=model)
    _emit("memory.dream.end", kind="refine",
          merged=len(out.get("merged", [])), kept=len(out.get("kept_separate", [])),
          candidates=out.get("candidates", 0),
          duration_ms=int((time.perf_counter() - t0) * 1000))
    return out


def _emit(event: str, **data: Any) -> None:
    """Best-effort dream telemetry (reuses the legacy memory.dream.* names)."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # pragma: no cover — telemetry must never break the dream
        pass


_SKILL_EXTRACT_PROMPT = """You are durin's skill extractor. Review the recent \
conversation(s) below. If the user established a REUSABLE PROCEDURE for a \
recurring task — a sequence of steps, a workflow, a how-to to follow again \
later — create or update a skill for it by calling the `skill_write` tool. A \
skill is a step-by-step procedure to FOLLOW, not a fact and not a one-off. \
Reuse/extend an existing skill instead of duplicating it. If the conversation \
contains no reusable procedure, do nothing — don't call any tool.

EXISTING SKILLS: {existing}
"""


def _recent_sessions_text(workspace: Path, max_sessions: int) -> str:
    """The newest sessions' conversation text (user + assistant turns)."""
    from durin.memory.extract_runner import load_session
    sdir = Path(workspace) / "sessions"
    if not sdir.is_dir():
        return ""
    files = sorted(sdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    blocks: list[str] = []
    for jsonl in files[:max_sessions]:
        _meta, msgs = load_session(jsonl)
        turns = "\n".join(
            f"{str(m.get('role') or '?').upper()}: {m.get('content')}"
            for m in msgs if m.get("content")
        )
        if turns.strip():
            blocks.append(f"=== session {jsonl.stem} ===\n{turns}")
    return "\n\n".join(blocks)[:12000]


def _list_skills(workspace: Path) -> list[str]:
    sdir = Path(workspace) / "skills"
    if not sdir.is_dir():
        return []
    return sorted(p.name for p in sdir.iterdir()
                  if p.is_dir() and (p / "SKILL.md").is_file())


def run_skill_extract_pass(
    workspace: Path,
    *,
    provider: Any | None = None,
    model: str | None = None,
    max_sessions: int = 3,
) -> dict:
    """Mine recent sessions for reusable procedures and create/update skills.

    The skills arm of the extract dream (design §2.7): an agentic sub-agent with
    ``skill_write`` writes a SKILL.md when a recurring procedure appears. Sync
    wrapper over the async AgentRunner flow (the cron calls it in a thread)."""
    import asyncio
    return asyncio.run(_skill_extract_async(
        workspace, provider=provider, model=model, max_sessions=max_sessions))


async def _skill_extract_async(
    workspace: Path, *, provider: Any | None, model: str | None, max_sessions: int,
) -> dict:
    sessions_text = _recent_sessions_text(workspace, max_sessions)
    if not sessions_text.strip():
        return {"skills_touched": 0, "reason": "no_sessions"}

    from durin.agent.runner import AgentRunner, AgentRunSpec
    from durin.agent.tools.file_state import FileStates
    from durin.agent.tools.filesystem import EditFileTool, ReadFileTool
    from durin.agent.tools.registry import ToolRegistry
    from durin.agent.tools.skill_write import SkillWriteTool

    fs = FileStates()
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=workspace, allowed_dir=workspace, file_states=fs))
    tools.register(EditFileTool(workspace=workspace, allowed_dir=workspace, file_states=fs))
    tools.register(SkillWriteTool(workspace=workspace))

    if provider is None:
        from durin.config.loader import load_config
        from durin.providers.factory import make_provider
        provider = make_provider(load_config())

    existing = _list_skills(workspace)
    messages = [
        {"role": "system",
         "content": _SKILL_EXTRACT_PROMPT.format(existing=", ".join(existing) or "(none)")},
        {"role": "user", "content": sessions_text},
    ]
    try:
        result = await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=messages, tools=tools, model=model or "glm-5.1",
            max_iterations=8, max_tool_result_chars=8000,
            fail_on_tool_error=False, workspace=Path(workspace),
        ))
    except Exception as exc:  # noqa: BLE001
        return {"skills_touched": 0, "error": str(exc)}

    touched = sum(1 for ev in (result.tool_events or [])
                  if ev.get("name") == "skill_write")
    _emit("memory.dream.skill_extract", skills_touched=touched)
    return {"skills_touched": touched}
