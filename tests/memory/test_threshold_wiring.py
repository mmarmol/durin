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
