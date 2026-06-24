from durin.personas.builtin import SEED_PERSONAS, seed_example_personas
from durin.config.schema import PersonaConfig
from durin.config.loader import get_config_path, load_config, mutate_config


def test_seed_personas_reference_soul_slugs():
    assert set(SEED_PERSONAS) == {"researcher", "engineer", "tutor"}
    for p in SEED_PERSONAS.values():
        assert isinstance(p, PersonaConfig)
        assert p.soul  # references a soul slug


def test_seed_writes_examples_and_sets_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    seed_example_personas()
    cfg = load_config(get_config_path())
    assert set(SEED_PERSONAS) <= set(cfg.personas)
    assert cfg.agents.defaults.personas_seeded is True


def test_seed_is_idempotent_and_respects_deletion(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    seed_example_personas()
    # A deleted example is not re-injected once the marker is set.
    mutate_config(lambda c: c.personas.pop("researcher", None))
    seed_example_personas()
    cfg = load_config(get_config_path())
    assert "researcher" not in cfg.personas
