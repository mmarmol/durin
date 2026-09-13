from durin.config.schema import AutoAbsorbConfig
from durin.memory import dream_passes


def test_semantic_distance_threshold_default():
    assert AutoAbsorbConfig().semantic_distance_threshold == 0.30


def test_run_refine_pass_forwards_threshold(monkeypatch):
    seen = {}

    def fake_run_refine(workspace, **kw):
        seen.update(kw)
        return {"merged": [], "kept_separate": [], "skipped": [], "candidates": 0}

    monkeypatch.setattr(dream_passes, "run_refine", fake_run_refine)
    dream_passes.run_refine_pass(
        "/tmp/x", enabled=True, semantic_distance_threshold=0.33,
        vector_index=object())
    assert seen["semantic_distance_threshold"] == 0.33


def test_run_extract_pass_forwards_thresholds_to_session(monkeypatch, tmp_path):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "s.jsonl").write_text(
        '{"_type":"metadata","key":"s"}\n', encoding="utf-8")
    seen = {}

    def fake_session(workspace, jsonl_path, **kw):
        seen.update(kw)
        return {"extracted": [], "discovered": [], "skill_signals": []}

    monkeypatch.setattr(dream_passes, "run_extract_for_session", fake_session)
    dream_passes.run_extract_pass(
        tmp_path, confidence_threshold=88, semantic_distance_threshold=0.27,
        vector_index=object())
    assert seen["confidence_threshold"] == 88
    assert seen["semantic_distance_threshold"] == 0.27


def test_auto_absorb_defaults_follow_the_measured_experience():
    """Escalation is on by default and the investigating judge has its own
    merge floor; the per-pass budget defaults to an hour."""
    from durin.config.schema import MemoryDreamConfig
    cfg = AutoAbsorbConfig()
    assert cfg.escalate_floor == 70
    assert cfg.tier2_confidence_threshold == 80
    assert cfg.confidence_threshold == 95
    assert MemoryDreamConfig().max_seconds_per_run == 3600


def test_run_refine_pass_forwards_the_tier2_floor(monkeypatch):
    seen = {}

    def fake_run_refine(workspace, **kw):
        seen.update(kw)
        return {"merged": [], "kept_separate": [], "skipped": [], "candidates": 0}

    monkeypatch.setattr(dream_passes, "run_refine", fake_run_refine)
    dream_passes.run_refine_pass("/tmp/x", enabled=True, escalate_floor=70,
                                 tier2_confidence_threshold=83, vector_index=object())
    assert seen["escalate_floor"] == 70 and seen["tier2_confidence_threshold"] == 83
