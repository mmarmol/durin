"""Code-defined personas always available without user config. Each references
a pre-seeded SOUL slug shipped into the workspace (see templates/souls/)."""
from durin.config.schema import PersonaConfig

BUILTIN_PERSONAS: dict[str, PersonaConfig] = {
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
