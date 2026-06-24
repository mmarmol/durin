from durin.personas.builtin import BUILTIN_PERSONAS
from durin.config.schema import PersonaConfig


def test_builtins_reference_soul_slugs():
    assert set(BUILTIN_PERSONAS) == {"researcher", "engineer", "tutor"}
    for p in BUILTIN_PERSONAS.values():
        assert isinstance(p, PersonaConfig)
        assert p.soul  # references a soul slug
