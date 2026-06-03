import durin.agent.context as ctxmod
from durin.agent.context import ContextBuilder


def _force_hot(monkeypatch, *, enabled=True, recent=1, frequent=1):
    from durin.config.schema import Config
    cfg = Config()
    cfg.memory.skills_hot_tier.enabled = enabled
    cfg.memory.skills_hot_tier.recent = recent
    cfg.memory.skills_hot_tier.frequent = frequent
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)


def _seed_skills(ws, names):
    for n in names:
        d = ws / "skills" / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {n}\ndescription: do {n}\n---\n# {n}\n")


def test_enabled_injects_only_working_set(tmp_path, monkeypatch):
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=True, recent=1, frequent=1)
    monkeypatch.setattr(ctxmod, "compute_working_set", lambda *a, **k: ["deploy", "rebase"])
    cb = ContextBuilder(tmp_path)
    cb.build_system_prompt()
    block = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "deploy" in block and "rebase" in block
    assert "obscure" not in block


def test_stable_across_turns(tmp_path, monkeypatch):
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=True, recent=1, frequent=1)
    monkeypatch.setattr(ctxmod, "compute_working_set", lambda *a, **k: ["deploy", "rebase"])
    cb = ContextBuilder(tmp_path)
    a = cb.build_system_prompt()
    b = cb.build_system_prompt()
    assert a == b


def test_disabled_injects_full_catalog(tmp_path, monkeypatch):
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=False)
    cb = ContextBuilder(tmp_path)
    cb.build_system_prompt()
    block = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "obscure" in block
