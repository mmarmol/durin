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
    sessions_dir = Path(workspace) / "sessions"
    out: dict[str, Any] = {"sessions": 0, "entities": 0, "errors": []}
    if not sessions_dir.is_dir():
        return out
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
    return out


def run_refine_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str = "glm-5.1",
) -> dict:
    """Run the refine dream (dedup duplicate entities). The daily cron entry."""
    return run_refine(workspace, llm_invoke=llm_invoke, model=model)
