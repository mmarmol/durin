"""`durin memory dream` hands the refine pass the configured first-judge effort."""
from __future__ import annotations

from durin.cli import memory_cmd
from durin.config.schema import Config, ModelPresetConfig


def test_the_manual_dream_passes_the_configured_judge_reasoning_effort(tmp_path, monkeypatch):
    import durin.memory.always_on_dream as always_on_dream
    import durin.memory.dream_passes as dream_passes
    import durin.memory.model_resolve as model_resolve

    seen: list[dict] = []
    monkeypatch.setattr(dream_passes, "dream_vector_index", lambda ws, cfg: None)
    monkeypatch.setattr(dream_passes, "run_extract_pass", lambda *a, **k: {"entities": 0, "sessions": 0})
    monkeypatch.setattr(dream_passes, "run_derived_from_pass", lambda *a, **k: {})
    monkeypatch.setattr(dream_passes, "run_skill_extract_pass", lambda *a, **k: {})
    monkeypatch.setattr(dream_passes, "run_refine_pass", lambda *a, **k: seen.append(k) or {})
    monkeypatch.setattr(always_on_dream, "run_always_on_pass", lambda *a, **k: {})
    monkeypatch.setattr(model_resolve, "resolve_aux_preset",
                        lambda *a, **k: ModelPresetConfig(model="m", provider="p"))
    monkeypatch.setattr(memory_cmd, "_workspace_root", lambda: tmp_path)

    cfg = Config()
    monkeypatch.setattr(memory_cmd, "load_config", lambda *a, **k: cfg)
    memory_cmd.cmd_dream(entity=None, dry_run=False)
    cfg.memory.dream.auto_absorb.judge_reasoning_effort = "low"
    memory_cmd.cmd_dream(entity=None, dry_run=False)

    assert [k["judge_reasoning_effort"] for k in seen] == [None, "low"]
