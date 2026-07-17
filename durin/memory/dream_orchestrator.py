"""Dream orchestration as plain sync functions that own their process.

The nightly consolidation (extract → derived-from → distill → seed → topics →
skill-extract → refine → relations → always-on → workflow-improve → skill
curation → suggestions) and the reactive extract were historically inlined in
the gateway's cron handler and a daemon thread. They live here as synchronous
entry points so any host — the gateway's dream worker subprocess, or the
manual CLI — can run them without an event loop: LLM calls, dulwich commits,
and embedding batches burn this process' CPU, not the gateway's.

Progress is reported through an injected ``progress`` callback receiving the
same payload dicts the websocket dream feed consumes (``run_started`` /
``activity`` / ``run_finished``); telemetry JSONL and the durable run record
are written by THIS process. Write-safety against concurrent processes is the
memory store's own contract (git-worktree flock + ref CAS, short critical
sections); ``dream_lock`` adds whole-run exclusion so two dreams never
interleave their passes.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from loguru import logger

from durin.config.schema import Config

Progress = Callable[[dict[str, Any]], None]


class DreamAlreadyRunningError(RuntimeError):
    """Another process (or thread) holds the dream lock."""


@contextmanager
def dream_lock(workspace: Path) -> Iterator[None]:
    """Whole-run mutual exclusion across processes (``<workspace>/.dream.lock``).

    Non-blocking: a held lock raises :class:`DreamAlreadyRunningError` immediately —
    the per-session cursors make a skipped run harmless, so waiting is never
    useful. Guards gateway-spawned workers and manual CLI dreams against each
    other (the in-gateway ``ReactiveDreamGate`` only ever covered one process).
    """
    from durin.utils.file_lock import cross_process_lock

    try:
        with cross_process_lock(Path(workspace) / ".dream", timeout=0):
            yield
    except TimeoutError as exc:
        raise DreamAlreadyRunningError(str(exc)) from exc


def _summary(ex: dict, rf: dict, sk: dict, skills_improved: int) -> dict[str, Any]:
    return {
        "sessions": ex.get("sessions", 0) if isinstance(ex, dict) else 0,
        "entities": ex.get("entities", 0) if isinstance(ex, dict) else 0,
        "merged": len(rf.get("merged", [])) if isinstance(rf, dict) else 0,
        "skills_created": sk.get("skills_touched", 0) if isinstance(sk, dict) else 0,
        "skills_improved": skills_improved,
    }


def run_full_dream(
    config: Config,
    workspace: Path,
    *,
    progress: Progress = lambda payload: None,
) -> dict[str, Any]:
    """Run the complete nightly consolidation. Returns the run summary.

    Emits ``run_started`` first and ``run_finished`` last (``ok`` reflecting
    the outcome) through ``progress``; raises ``RuntimeError`` after the
    finish event when a consolidation pass failed, so a supervising caller
    records an error status instead of a false success.
    """
    workspace = Path(workspace)
    from durin.memory.always_on_dream import run_always_on_pass
    from durin.memory.distill_dream import (
        run_curate_topics_pass,
        run_distill_reference_pass,
        run_seed_entities_pass,
    )
    from durin.memory.dream_live import DreamProgressSink
    from durin.memory.dream_passes import (
        dream_vector_index,
        run_derived_from_pass,
        run_extract_pass,
        run_refine_pass,
        run_skill_extract_pass,
    )
    from durin.memory.model_resolve import resolve_aux_preset
    from durin.telemetry.logger import bind_telemetry, get_session_logger, reset_telemetry
    from durin.workflow.workflow_improve_dream import run_workflow_improve_pass

    # One resolution for the whole run: the memory preset pairs the model WITH
    # its provider; the passes' default_llm_invoke resolves the same preset.
    model = resolve_aux_preset(config, purpose="memory").model
    max_s = config.memory.dream.max_seconds_per_run
    absorb = config.memory.dream.auto_absorb
    run_started_at = datetime.now(timezone.utc)
    vi = dream_vector_index(workspace, config)

    tlog = get_session_logger("cron_dream")
    tlog.add_sink(DreamProgressSink(progress))
    ttok = bind_telemetry(tlog)
    ex: dict = {}
    sk: dict = {}
    rf: dict = {}
    dream_error: Exception | None = None
    skills_improved = 0
    progress({"kind": "run_started"})
    try:
        try:
            ex = run_extract_pass(
                workspace, model=model, max_seconds=max_s,
                discover=config.memory.dream.discover_enabled,
                skill_signals=config.memory.dream.skill_signals_enabled,
                learnings=config.memory.dream.learnings_sweep_enabled,
                confidence_threshold=absorb.confidence_threshold,
                semantic_distance_threshold=absorb.semantic_distance_threshold,
                vector_index=vi)
            df = run_derived_from_pass(workspace, model=model, max_seconds=max_s)
            # Distil ingested reference documents into outline sidecars — the
            # "know the book" index. Independent of entity merges, so it slots
            # right after the source-link pass.
            di = (
                run_distill_reference_pass(workspace, model=model, max_seconds=max_s)
                if config.memory.dream.distill_references_enabled
                else {"references": 0, "outlined": 0, "skipped": 0, "duration_ms": 0}
            )
            # Seed candidate entities from each distilled document's outline
            # (derived_from = the document). Reads outlines the distil step just
            # wrote, so it follows it. The refine pass dedups later.
            se = (
                run_seed_entities_pass(workspace, model=model, max_seconds=max_s)
                if config.memory.dream.seed_entities_from_docs_enabled
                else {"references": 0, "seeded_docs": 0, "entities": 0,
                      "skipped": 0, "duration_ms": 0}
            )
            # Curate the library's topic index from the distilled abstracts —
            # the clean, stable "Covers:" map the always-on awareness reads.
            ct = (
                run_curate_topics_pass(workspace, model=model, max_seconds=max_s)
                if config.memory.dream.curate_topics_enabled
                else {"topics": 0, "skipped": True, "duration_ms": 0}
            )
            logger.info(
                "memory_dream: distill(references={} outlined={} skipped={} {}ms) "
                "seed_entities(docs={} entities={} {}ms) topics(n={} {}ms)",
                di.get("references", 0), di.get("outlined", 0),
                di.get("skipped", 0), di.get("duration_ms", 0),
                se.get("seeded_docs", 0), se.get("entities", 0),
                se.get("duration_ms", 0),
                ct.get("topics", 0), ct.get("duration_ms", 0))
            sk = run_skill_extract_pass(workspace, model=model)
            rf = run_refine_pass(
                workspace, model=model,
                enabled=absorb.enabled,
                confidence_threshold=absorb.confidence_threshold,
                escalate_floor=absorb.escalate_floor,
                semantic_distance_threshold=absorb.semantic_distance_threshold,
                run_started_at=run_started_at,
                vector_index=vi)
            # Relation-vocabulary hygiene: canonicalise entity-relation type
            # labels so graph edges line up. Runs after refine (which merges
            # entities and their relations).
            from durin.memory.relation_hygiene import run_consolidate_relations_pass
            rh = (
                run_consolidate_relations_pass(workspace, max_seconds=max_s)
                if config.memory.dream.consolidate_relations_enabled
                else {"types_before": 0, "types_after": 0,
                      "pages_changed": 0, "merged_duplicates": 0, "duration_ms": 0}
            )
            logger.info(
                "memory_dream: relations(types {}→{} pages={} merged={} {}ms)",
                rh.get("types_before", 0), rh.get("types_after", 0),
                rh.get("pages_changed", 0), rh.get("merged_duplicates", 0),
                rh.get("duration_ms", 0))
            ao = run_always_on_pass(
                workspace, model=model,
                token_budget=config.memory.dream.always_on_token_budget)
            # Workflow self-improvement: inert unless a workflow opts into
            # improvement_mode 'manual'/'auto' (off by default).
            wi = run_workflow_improve_pass(workspace, model=model)
            logger.info(
                "memory_dream: workflow_improve(workflows={} proposals={})",
                wi.get("workflows", 0), wi.get("proposals", 0))
            logger.info(
                "memory_dream: extract(sessions={} entities={} {}ms yielded={}) "
                "derived_from(links={} sessions={} {}ms) "
                "skills(touched={} {}ms) refine(merged={} kept={} {}ms) "
                "always_on(pinned={} {}tok {}ms)",
                ex["sessions"], ex["entities"], ex.get("duration_ms", 0),
                ex.get("yielded", False),
                df.get("links", 0), df.get("sessions", 0), df.get("duration_ms", 0),
                sk.get("skills_touched", 0),
                sk.get("duration_ms", 0), len(rf.get("merged", [])),
                len(rf.get("kept_separate", [])), rf.get("duration_ms", 0),
                ao.get("selected", 0), ao.get("tokens", 0), ao.get("duration_ms", 0),
            )
        except Exception as exc:  # noqa: BLE001 — recorded, re-raised after teardown
            logger.exception("memory_dream consolidation failed")
            dream_error = exc

        # Skills improved by the curation pass (edits applied to existing
        # skills) — distinct from skills CREATED by the skill-extract pass.
        try:
            from durin.agent.skill_curation import curate_catalog
            from durin.agent.skill_drift import check_upstream_drift
            from durin.agent.skill_usage import collect_recent_skill_calls
            from durin.memory.llm_invoke import default_llm_invoke

            def _judge(prompt: str) -> str:
                # ONE completion via the memory preset (model + provider),
                # the same call shape the refine pass's absorb judge uses.
                return default_llm_invoke(prompt).text

            usage = collect_recent_skill_calls(workspace, within_hours=24)
            summary = curate_catalog(
                workspace, judge=_judge, usage=usage,
                drift_check=check_upstream_drift,
                allowlist=list(config.skills.security.allowlist))
            skills_improved = summary.get("applied", 0)
            obs = summary.get("observations", {})
            logger.info(
                "skill curation: reviewed={} applied={} deferred={} backfilled={} "
                "judge_parse_failed={} "
                "obs_applied={} obs_declined={} obs_kept={} obs_open={} principles={}",
                summary["reviewed"], summary["applied"], summary["deferred"],
                summary.get("backfilled", 0), summary.get("judge_parse_failed", False),
                obs.get("applied", 0), obs.get("declined", 0), obs.get("kept", 0),
                obs.get("open", 0), summary.get("principles", 0),
            )
        except Exception:
            logger.exception("skill curation step (non-fatal) failed")

        # Skill suggestions for MANUAL skills: propose curation actions into
        # the dream bandeja for user review (never auto-applied). Gated +
        # best-effort: a failure here must not abort the dream.
        if config.memory.dream.skill_suggestions_enabled:
            try:
                from durin.agent.skill_curation import suggest_manual_skills
                from durin.agent.skill_usage import collect_recent_skill_calls
                from durin.memory.llm_invoke import default_llm_invoke

                def _sg_judge(prompt: str) -> str:
                    return default_llm_invoke(prompt).text

                sg_usage = collect_recent_skill_calls(workspace, within_hours=24)
                sg = suggest_manual_skills(workspace, judge=_sg_judge, usage=sg_usage)
                logger.info(
                    "skill suggestions: reviewed={} suggested={} suppressed={}",
                    sg["reviewed"], sg["suggested"], sg["suppressed"])
            except Exception:
                logger.exception("skill suggestions step (non-fatal) failed")

        # One summary entry per run — even an empty run leaves a visible
        # "ran, nothing new" line in the Dream feed instead of silently
        # updating only the last-run time.
        from durin.agent.tools._telemetry import emit_tool_event
        from durin.memory.dream_runs import record_dream_run

        run_summary = _summary(ex, rf, sk, skills_improved)
        emit_tool_event("memory.dream.run_summary", run_summary)
        # Durable record so the "last run" card + history survive the
        # telemetry window / retention (telemetry is the live feed; this is
        # the truth).
        record_dream_run(workspace, run_summary)
    finally:
        reset_telemetry(ttok)
        progress({"kind": "run_finished", "ok": dream_error is None})

    if dream_error is not None:
        # Surface the failure so the supervising caller records status="error"
        # (not a false "ok") — the consolidation passes did not complete.
        raise RuntimeError(
            f"memory_dream consolidation failed: {dream_error}"
        ) from dream_error
    return run_summary


def run_reactive_dream(
    config: Config,
    workspace: Path,
    *,
    trigger: str,
    progress: Progress = lambda payload: None,
) -> dict[str, Any]:
    """Reactive EXTRACT — when a session closes or compacts, extract its new
    turns into entity attributes immediately (the frequent dream,
    event-driven; the per-session cursor makes it idempotent). Refine stays
    on the full nightly run."""
    import time

    workspace = Path(workspace)
    from durin.agent.tools._telemetry import emit_tool_event
    from durin.memory.dream_live import DreamProgressSink
    from durin.memory.dream_passes import dream_vector_index, run_extract_pass
    from durin.memory.dream_runs import record_dream_run
    from durin.memory.model_resolve import resolve_aux_preset
    from durin.telemetry.logger import bind_telemetry, get_session_logger, reset_telemetry

    t_run = time.perf_counter()
    tlog = get_session_logger("reactive_dream")
    tlog.add_sink(DreamProgressSink(progress))
    rtok = bind_telemetry(tlog)
    error: Exception | None = None
    out: dict = {}
    progress({"kind": "run_started"})
    try:
        out = run_extract_pass(
            workspace,
            model=resolve_aux_preset(config, purpose="memory").model,
            max_seconds=config.memory.dream.max_seconds_per_run,
            discover=config.memory.dream.discover_enabled,
            skill_signals=config.memory.dream.skill_signals_enabled,
            learnings=config.memory.dream.learnings_sweep_enabled,
            confidence_threshold=config.memory.dream.auto_absorb.confidence_threshold,
            semantic_distance_threshold=config.memory.dream.auto_absorb.semantic_distance_threshold,
            vector_index=dream_vector_index(workspace, config),
        )
        logger.info(
            "reactive dream done ({}): {} session(s), {} attribute update(s), "
            "yielded={}, {}ms",
            trigger, out.get("sessions", 0), out.get("entities", 0),
            out.get("yielded", False),
            int((time.perf_counter() - t_run) * 1000),
        )
        # Record a run summary so reactive runs also surface in the Dream
        # feed / last-run card. The reactive path is extract-only.
        run_summary = {
            "sessions": out.get("sessions", 0),
            "entities": out.get("entities", 0),
            "merged": 0,
            "skills_created": 0,
            "skills_improved": 0,
        }
        emit_tool_event("memory.dream.run_summary", run_summary)
        record_dream_run(workspace, run_summary)
    except Exception as exc:  # noqa: BLE001 — recorded, re-raised after teardown
        logger.exception("reactive dream failed ({})", trigger)
        error = exc
    finally:
        reset_telemetry(rtok)
        progress({"kind": "run_finished", "ok": error is None})

    if error is not None:
        raise RuntimeError(f"reactive dream failed: {error}") from error
    return run_summary
