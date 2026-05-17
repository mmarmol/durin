"""Deliberation system configuration — re-exports from config.schema."""

from durin.config.schema import (
    DeliberationConfig,
    EvaluatorConfig,
    GeneratorRoleConfig,
)

__all__ = ["DeliberationConfig", "EvaluatorConfig", "GeneratorRoleConfig"]
