import durin.agent.context as ctxmod
from durin.agent.context import ContextBuilder


def _force_hot(monkeypatch, *, enabled=True, recent=1, frequent=1):
    from durin.config.schema import Config
    cfg = Config()
    cfg.agents.defaults.skills_hot_tier.enabled = enabled
    cfg.agents.defaults.skills_hot_tier.recent = recent
    cfg.agents.defaults.skills_hot_tier.frequent = frequent
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)


def _seed_skills(ws, names):
    prov = ("metadata:\n  durin:\n    provenance:\n"
            '      source: "github:o/r/x"\n      content_hash: "abc"\n')
    for n in names:
        d = ws / "skills" / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {n}\ndescription: do {n}\n{prov}---\n# {n}\n")


def _seed_usage(ws, calls, *, name="s1"):
    """Write one session sidecar with derived.skill_calls. calls: {skill: count}."""
    import json
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    skill_calls = []
    for skill, n in calls.items():
        skill_calls.extend([{"skill": skill, "op": "read"}] * n)
    (sdir / f"{name}.meta.json").write_text(
        json.dumps({"session_key": name, "events": [],
                    "derived": {"skill_calls": skill_calls}})
    )


def test_enabled_injects_only_working_set(tmp_path, monkeypatch):
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=True, recent=1, frequent=1)
    monkeypatch.setattr(ctxmod, "compute_working_set", lambda *a, **k: ["deploy", "rebase"])
    cb = ContextBuilder(tmp_path)
    cb.build_system_prompt()
    block = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "deploy" in block and "rebase" in block
    assert "obscure" not in block


def test_working_set_memoized_against_usage_drift(tmp_path, monkeypatch):
    # Uses the REAL compute_working_set (no patch). Mutates usage on disk
    # BETWEEN two builds on the same ContextBuilder; the memoized working set
    # must NOT change → stable layer byte-identical. A broken memo (recompute
    # per turn) would promote `obscure` and drop `rebase`, failing this.
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=True, recent=1, frequent=1)  # budget 2
    _seed_usage(tmp_path, {"deploy": 10, "rebase": 8}, name="s1")
    cb = ContextBuilder(tmp_path)
    a = cb.build_system_prompt()
    block_a = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "deploy" in block_a and "rebase" in block_a and "obscure" not in block_a

    # Heavy new usage of the previously-excluded skill lands mid-session.
    _seed_usage(tmp_path, {"obscure": 100}, name="s2")
    b = cb.build_system_prompt()
    block_b = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert b == a                       # whole stable layer frozen
    assert block_b == block_a           # working set frozen
    assert "obscure" not in block_b     # drift did NOT leak in
    assert "rebase" in block_b          # and the original member stayed


def test_working_set_recomputes_when_skill_installed(tmp_path, monkeypatch):
    # The memo is keyed on the candidate name-set: a skill installed on disk
    # between two builds on the SAME ContextBuilder (the gateway keeps one per
    # process) must reach the catalog without a restart.
    _seed_skills(tmp_path, ["deploy"])
    _force_hot(monkeypatch, enabled=True, recent=1, frequent=1)  # budget 2
    cb = ContextBuilder(tmp_path)
    cb.build_system_prompt()
    block_a = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "deploy" in block_a and "zzznew" not in block_a

    _seed_skills(tmp_path, ["zzznew"])  # installed mid-process
    cb.build_system_prompt()
    block_b = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "zzznew" in block_b           # entered without a restart
    assert "deploy" in block_b          # existing member kept its slot


def test_working_set_recomputes_when_skill_removed(tmp_path, monkeypatch):
    import shutil
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=True, recent=1, frequent=1)  # budget 2
    _seed_usage(tmp_path, {"deploy": 10, "rebase": 8}, name="s1")
    cb = ContextBuilder(tmp_path)
    cb.build_system_prompt()
    block_a = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "obscure" not in block_a

    shutil.rmtree(tmp_path / "skills" / "rebase")  # removed mid-process
    cb.build_system_prompt()
    block_b = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "rebase" not in block_b      # gone from the catalog
    assert "obscure" in block_b         # freed slot refilled


def test_disabled_injects_full_catalog(tmp_path, monkeypatch):
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=False)
    cb = ContextBuilder(tmp_path)
    cb.build_system_prompt()
    block = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "obscure" in block
