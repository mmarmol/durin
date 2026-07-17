"""Tests for the extracted dream orchestration (durin/memory/dream_orchestrator.py)."""

from __future__ import annotations

import pytest

from durin.config.schema import Config


def _mk_stub(calls, name, ret):
    def f(*args, **kwargs):
        calls.append(name)
        return dict(ret)

    return f


@pytest.fixture()
def stubbed_passes(monkeypatch):
    """Stub every dream pass at its source module; return the call recorder."""
    calls: list[str] = []
    import durin.agent.skill_curation as skill_curation
    import durin.agent.skill_usage as skill_usage
    import durin.memory.always_on_dream as always_on_dream
    import durin.memory.distill_dream as distill_dream
    import durin.memory.dream_passes as dream_passes
    import durin.memory.model_resolve as model_resolve
    import durin.memory.relation_hygiene as relation_hygiene
    import durin.workflow.workflow_improve_dream as workflow_improve_dream

    monkeypatch.setattr(dream_passes, "dream_vector_index", lambda ws, cfg: None)
    monkeypatch.setattr(
        dream_passes,
        "run_extract_pass",
        _mk_stub(calls, "extract", {"sessions": 2, "entities": 3, "yielded": False}),
    )
    monkeypatch.setattr(
        dream_passes,
        "run_derived_from_pass",
        _mk_stub(calls, "derived_from", {"links": 1, "sessions": 1}),
    )
    monkeypatch.setattr(
        distill_dream,
        "run_distill_reference_pass",
        _mk_stub(calls, "distill", {"references": 0, "outlined": 0, "skipped": 0}),
    )
    monkeypatch.setattr(
        distill_dream,
        "run_seed_entities_pass",
        _mk_stub(calls, "seed", {"references": 0, "seeded_docs": 0, "entities": 0, "skipped": 0}),
    )
    monkeypatch.setattr(
        distill_dream,
        "run_curate_topics_pass",
        _mk_stub(calls, "topics", {"topics": 0}),
    )
    monkeypatch.setattr(
        dream_passes,
        "run_skill_extract_pass",
        _mk_stub(calls, "skill_extract", {"skills_touched": 4}),
    )
    monkeypatch.setattr(
        dream_passes,
        "run_refine_pass",
        _mk_stub(calls, "refine", {"merged": ["a", "b"], "kept_separate": []}),
    )
    monkeypatch.setattr(
        relation_hygiene,
        "run_consolidate_relations_pass",
        _mk_stub(calls, "relations", {"types_before": 0, "types_after": 0,
                                      "pages_changed": 0, "merged_duplicates": 0}),
    )
    monkeypatch.setattr(
        always_on_dream,
        "run_always_on_pass",
        _mk_stub(calls, "always_on", {"selected": 0, "tokens": 0}),
    )
    monkeypatch.setattr(
        workflow_improve_dream,
        "run_workflow_improve_pass",
        _mk_stub(calls, "workflow_improve", {"workflows": 0, "proposals": 0}),
    )
    monkeypatch.setattr(
        skill_curation,
        "curate_catalog",
        _mk_stub(calls, "curate", {"reviewed": 0, "applied": 5, "deferred": 0,
                                   "observations": {}}),
    )
    monkeypatch.setattr(
        skill_curation,
        "suggest_manual_skills",
        _mk_stub(calls, "suggest", {"reviewed": 0, "suggested": 0, "suppressed": 0}),
    )
    monkeypatch.setattr(skill_usage, "collect_recent_skill_calls", lambda *a, **k: [])

    class _Preset:
        model = "test-model"

    monkeypatch.setattr(model_resolve, "resolve_aux_preset", lambda *a, **k: _Preset())
    return calls


def _cfg() -> Config:
    cfg = Config()
    cfg.memory.dream.distill_references_enabled = True
    cfg.memory.dream.seed_entities_from_docs_enabled = True
    cfg.memory.dream.curate_topics_enabled = True
    cfg.memory.dream.consolidate_relations_enabled = True
    cfg.memory.dream.skill_suggestions_enabled = True
    return cfg


def test_full_dream_order_summary_and_progress(stubbed_passes, tmp_path):
    from durin.memory.dream_orchestrator import run_full_dream

    events: list[dict] = []
    summary = run_full_dream(_cfg(), tmp_path, progress=events.append)

    assert stubbed_passes == [
        "extract",
        "derived_from",
        "distill",
        "seed",
        "topics",
        "skill_extract",
        "refine",
        "relations",
        "always_on",
        "workflow_improve",
        "curate",
        "suggest",
    ]
    assert summary == {
        "sessions": 2,
        "entities": 3,
        "merged": 2,
        "skills_created": 4,
        "skills_improved": 5,
    }
    assert events[0] == {"kind": "run_started"}
    assert events[-1] == {"kind": "run_finished", "ok": True}


def test_full_dream_pass_failure_raises_after_finish_event(stubbed_passes, tmp_path, monkeypatch):
    import durin.memory.dream_passes as dream_passes
    from durin.memory.dream_orchestrator import run_full_dream

    def boom(*a, **k):
        raise ValueError("pass exploded")

    monkeypatch.setattr(dream_passes, "run_extract_pass", boom)

    events: list[dict] = []
    with pytest.raises(RuntimeError):
        run_full_dream(_cfg(), tmp_path, progress=events.append)
    assert events[0] == {"kind": "run_started"}
    assert events[-1] == {"kind": "run_finished", "ok": False}


def test_reactive_dream_extract_only(stubbed_passes, tmp_path):
    from durin.memory.dream_orchestrator import run_reactive_dream

    events: list[dict] = []
    summary = run_reactive_dream(
        _cfg(), tmp_path, trigger="session_close", progress=events.append
    )
    assert stubbed_passes == ["extract"]
    assert summary == {
        "sessions": 2,
        "entities": 3,
        "merged": 0,
        "skills_created": 0,
        "skills_improved": 0,
    }
    assert events[0] == {"kind": "run_started"}
    assert events[-1] == {"kind": "run_finished", "ok": True}


def test_dream_lock_exclusive(tmp_path):
    """A held dream lock rejects a second acquirer. cross_process_lock is
    reentrant per thread, so the contender must run on another thread (flock
    conflicts between distinct fds even within one process)."""
    import threading

    from durin.memory.dream_orchestrator import DreamAlreadyRunningError, dream_lock

    outcome: dict = {}

    def contender():
        try:
            with dream_lock(tmp_path):
                outcome["acquired"] = True
        except DreamAlreadyRunningError:
            outcome["rejected"] = True

    with dream_lock(tmp_path):
        t = threading.Thread(target=contender)
        t.start()
        t.join(timeout=10)
    assert outcome == {"rejected": True}
