"""Cron entry points for the memory dream passes.

There is ONE dream cron — ``memory_dream`` (daily, ``memory.dream.cron``) — plus
the reactive triggers (post-compaction / session-close, ``ReactiveDreamGate``),
which run the extract pass only. The daily cron runs, in order: extract →
skill-extract → refine → always_on → curate_catalog. This module hosts
``run_extract_pass``, ``run_skill_extract_pass`` and ``run_refine_pass``;
``run_always_on_pass`` lives in ``always_on_dream`` and ``curate_catalog`` in
``agent.skill_curation``.

These REPLACE the legacy ``DreamRunner`` / ``DreamConsolidator`` (which
consolidated episodic entries into pages via JSON-Patch + working-tree writes —
the obsolete model + the G3 race). All passes write through ``memory_writer``
(plumbing + CAS).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from durin.memory.extract_runner import run_extract_for_session
from durin.memory.refine_dream import run_refine

__all__ = ["run_extract_pass", "run_skill_extract_pass", "run_refine_pass", "ReactiveDreamGate"]

LLMInvoke = Callable[..., Any]


class ReactiveDreamGate:
    """In-process concurrency lock + throttle for the reactive dream triggers.

    The post_compaction / on_session_close triggers fire on a daemon thread per
    event (same gateway process). Without a guard, a burst of session closes
    would spawn a burst of overlapping extract passes — duplicated LLM cost and
    thread pile-up. This replaces the cross-process ``.dream.lock`` + throttle
    the legacy ``DreamRunner`` owned (removed §8e). One instance is shared by
    all reactive triggers in a gateway.

    ``try_begin`` is non-blocking: it returns False (skip this run) when a pass
    is already in progress, or when one completed within ``min_seconds``. The
    per-session cursor makes a skipped run harmless — its turns are picked up by
    the in-flight pass, the next trigger, or the daily cron.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_end = 0.0  # monotonic; 0 → first run always allowed

    def try_begin(self, min_seconds: float) -> str:
        """Return "" when the caller may run, else a skip reason for telemetry:
        ``"locked"`` (a pass is already running) or ``"throttled"`` (one ran
        within ``min_seconds``)."""
        if not self._lock.acquire(blocking=False):
            return "locked"
        if min_seconds and self._last_end and (time.monotonic() - self._last_end) < min_seconds:
            self._lock.release()
            return "throttled"
        return ""

    def end(self) -> None:
        self._last_end = time.monotonic()
        self._lock.release()


def run_extract_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    max_seconds: int = 0,
) -> dict:
    """Run the extract dream over every session that has new turns.

    Per-session cursors make this idempotent — a session with no new turns is
    skipped. Best-effort per session: one bad session doesn't abort the pass.

    ``max_seconds`` (0 = unbounded) is a hard wall-clock cap: when the elapsed
    time crosses it the pass yields after the current session, and the cursor
    resumes the remainder on the next trigger (``memory.dream.max_seconds_per_run``).
    """
    import time
    t0 = time.perf_counter()
    _emit("memory.dream.start", kind="extract")
    sessions_dir = Path(workspace) / "sessions"
    out: dict[str, Any] = {"sessions": 0, "entities": 0, "errors": [], "yielded": False}
    if sessions_dir.is_dir():
        for jsonl_path in sorted(sessions_dir.glob("*.jsonl")):
            if max_seconds and (time.perf_counter() - t0) >= max_seconds:
                out["yielded"] = True
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                _emit("memory.dream.max_seconds_reached", kind="extract",
                      max_seconds=max_seconds, elapsed_ms=elapsed_ms,
                      sessions_done=out["sessions"])
                logger.warning(
                    "extract dream hit max_seconds_per_run ({}s) after {} session(s) "
                    "in {}ms; the per-session cursor resumes the remainder on the "
                    "next trigger", max_seconds, out["sessions"], elapsed_ms)
                break
            try:
                r = run_extract_for_session(
                    workspace, jsonl_path, llm_invoke=llm_invoke, model=model)
                extracted = r.get("extracted") or []
                if extracted:
                    out["sessions"] += 1
                    out["entities"] += len(extracted)
            except Exception as exc:  # noqa: BLE001 — never abort the whole pass
                out["errors"].append({"session": jsonl_path.stem, "error": str(exc)})
    out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    _emit("memory.dream.end", kind="extract",
          entities_consolidated=out["entities"], entities_failed=len(out["errors"]),
          sessions=out["sessions"], yielded=out["yielded"],
          duration_ms=out["duration_ms"])
    return out


def run_refine_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str = "glm-5.1",
    enabled: bool = True,
    confidence_threshold: int = 95,
    min_age_hours: int = 0,
) -> dict:
    """Run the refine dream (dedup duplicate entities). The daily cron entry.

    ``enabled`` gates the AUTO-merge: when False (the conservative default of
    ``memory.dream.auto_absorb.enabled``) the pass does NOT merge — duplicates
    are surfaced on demand by ``durin memory absorb-suggest`` and merged with
    ``durin memory absorb``. ``confidence_threshold`` is the LLM-judge floor for
    an auto-merge; ``min_age_hours`` quarantines freshly-created entities. All
    three are wired from config by the cron / manual callers.
    """
    import time
    t0 = time.perf_counter()
    if not enabled:
        logger.info(
            "refine dream skipped: auto_absorb disabled (default). Duplicates are "
            "surfaced by 'durin memory absorb-suggest'; merge with 'durin memory absorb'."
        )
        return {"merged": [], "kept_separate": [], "skipped": [],
                "candidates": 0, "disabled": True, "duration_ms": 0}
    _emit("memory.dream.start", kind="refine")
    out = run_refine(workspace, llm_invoke=llm_invoke, model=model,
                     confidence_threshold=confidence_threshold,
                     min_age_hours=min_age_hours)
    out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    _emit("memory.dream.end", kind="refine",
          merged=len(out.get("merged", [])), kept=len(out.get("kept_separate", [])),
          candidates=out.get("candidates", 0),
          duration_ms=out["duration_ms"])
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
later — capture it as a skill. A skill is a step-by-step procedure to FOLLOW, \
not a fact and not a one-off.

For each reusable procedure you find, prefer acquiring an existing published \
skill over writing one from scratch:
1. Call `skill_search` with a short query to see if a registry already has it.
2. If a strong hit exists, call `skill_acquire_seed` with that hit's ref. The \
security gate runs automatically, so you only ever receive SAFE, allowlisted \
content; a risky or un-allowlisted ref returns {{"seed": null}}. If you get a \
seed, adapt it to the conversation and save it with `skill_write`.
3. If search finds nothing usable, or `skill_acquire_seed` returns null, AUTHOR \
the skill yourself from the conversation via `skill_write`.

Reuse/extend an existing LOCAL skill instead of duplicating it. If the \
conversation contains no reusable procedure, do nothing — don't call any tool.

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


def _build_skill_extract_tools(workspace: Path, fs: Any) -> Any:
    """Toolset for the skill-extract sub-agent.

    Two ways to land a skill: AUTHOR from the conversation (Read/Edit/SkillWrite)
    or ACQUIRE a published one autonomously — Path B of acquire-on-gap (§6.C):
    ``skill_search`` finds a candidate, ``skill_acquire_seed`` pulls a SAFE seed.
    The seed gate runs in code (allowlist + scan), so a risky / un-allowlisted ref
    is never handed back; the default empty allowlist means nothing auto-seeds and
    the dream just authors from scratch. Path B previously lived in the deleted 2h
    ``Dream`` phase-2; the daily ``memory_dream`` skill-extract pass is its new home.
    """
    from durin.agent.tools.filesystem import EditFileTool, ReadFileTool
    from durin.agent.tools.registry import ToolRegistry
    from durin.agent.tools.skill_acquire_seed import SkillAcquireSeedTool
    from durin.agent.tools.skill_search import SkillSearchTool
    from durin.agent.tools.skill_write import SkillWriteTool

    registries: list = []
    allowlist: list[str] = []
    limit = 10
    try:
        from durin.config.loader import load_config
        sk = load_config().skills
        registries = list(sk.discovery.registries)
        limit = int(sk.discovery.search_limit)
        allowlist = list(sk.security.allowlist)
    except Exception:  # noqa: BLE001
        pass  # default config (empty allowlist) → acquire is a no-op, author-only

    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=workspace, allowed_dir=workspace, file_states=fs))
    tools.register(EditFileTool(workspace=workspace, allowed_dir=workspace, file_states=fs))
    tools.register(SkillWriteTool(workspace=workspace))
    tools.register(SkillSearchTool(workspace=workspace, registries=registries,
                                   allowlist=allowlist, limit=limit))
    tools.register(SkillAcquireSeedTool(workspace=workspace, allowlist=allowlist))
    return tools


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
    t0 = time.perf_counter()
    sessions_text = _recent_sessions_text(workspace, max_sessions)
    if not sessions_text.strip():
        return {"skills_touched": 0, "reason": "no_sessions"}

    from durin.agent.runner import AgentRunner, AgentRunSpec
    from durin.agent.tools.file_state import FileStates

    fs = FileStates()
    tools = _build_skill_extract_tools(workspace, fs)

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
    duration_ms = int((time.perf_counter() - t0) * 1000)
    _emit("memory.dream.skill_extract", skills_touched=touched, duration_ms=duration_ms)
    logger.info("skill-extract dream: {} skill(s) touched in {}ms", touched, duration_ms)
    return {"skills_touched": touched, "duration_ms": duration_ms}
