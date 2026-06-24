"""Example personas seeded into a fresh config as normal, fully editable and
deletable user personas (NOT an immutable built-in category). Each references a
pre-seeded SOUL slug shipped into the workspace (see templates/souls/)."""
from loguru import logger

from durin.config.schema import PersonaConfig

SEED_PERSONAS: dict[str, PersonaConfig] = {
    "researcher": PersonaConfig(
        soul="researcher",
        description="Rigorous research analyst (adapted from academic-research-skills).",
    ),
    "engineer": PersonaConfig(
        soul="engineer",
        description="Terse senior engineer with strong taste (Hermes-style).",
    ),
    "tutor": PersonaConfig(
        soul="tutor",
        description="Socratic tutor (adapted from OpenAI's Socratic prompt / Mr. Ranedeer).",
    ),
}


def seed_example_personas() -> None:
    """One-time seed of the example personas into the user's config as ordinary
    personas. Idempotent via the ``agents.defaults.personas_seeded`` marker, so a
    user who edits or deletes a seeded example keeps that choice across restarts
    (the examples are never re-injected once the marker is set). Best-effort:
    never raises, so a config-write hiccup cannot block startup."""
    from durin.config.loader import get_config_path, load_config, mutate_config

    def _seed(c: object) -> None:
        for name, persona in SEED_PERSONAS.items():
            c.personas.setdefault(name, persona)
        c.agents.defaults.personas_seeded = True

    try:
        if load_config(get_config_path()).agents.defaults.personas_seeded:
            return
        mutate_config(_seed)
    except Exception as e:
        logger.warning("Could not seed example personas: {}", e)
