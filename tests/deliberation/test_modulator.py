"""Tests for structural modulation of generators based on posture."""

from __future__ import annotations

from durin.deliberation.generator import GeneratorConfig
from durin.deliberation.modulator import (
    _DISCIPLINA_HIGH_SUFFIX,
    _EXTRA_PERMISSION_EXPLORADOR,
    modulate_generators,
    phrase_from_snapshot,
)
from durin.deliberation.types import GeneratorRole


def _default_generators() -> list[GeneratorConfig]:
    return [
        GeneratorConfig(role=GeneratorRole.PRAGMATICO, model="m", temperature=0.3, prompt_template="pragmatico base"),
        GeneratorConfig(role=GeneratorRole.EXPLORADOR, model="m", temperature=0.8, prompt_template="explorador base"),
        GeneratorConfig(role=GeneratorRole.CRITICO, model="m", temperature=0.5, prompt_template="critico base"),
    ]


class TestProfundidadFiltersCritico:
    def test_critico_removed_when_profundidad_low(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"profundidad": 0.2})
        roles = [g.role for g in result]
        assert GeneratorRole.CRITICO not in roles
        assert GeneratorRole.PRAGMATICO in roles
        assert GeneratorRole.EXPLORADOR in roles

    def test_critico_kept_when_profundidad_normal(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"profundidad": 0.5})
        roles = [g.role for g in result]
        assert GeneratorRole.CRITICO in roles

    def test_critico_kept_when_profundidad_high(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"profundidad": 0.9})
        roles = [g.role for g in result]
        assert GeneratorRole.CRITICO in roles


class TestExploracionTemperature:
    def test_explorador_temp_increases_with_high_exploration(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"exploracion": 0.9})
        explorador = next(g for g in result if g.role == GeneratorRole.EXPLORADOR)
        # base 0.8 + 0.3*(0.9-0.5) = 0.8 + 0.12 = 0.92
        assert explorador.temperature > 0.8

    def test_explorador_temp_decreases_with_low_exploration(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"exploracion": 0.2})
        explorador = next(g for g in result if g.role == GeneratorRole.EXPLORADOR)
        # base 0.8 + 0.3*(0.2-0.5) = 0.8 - 0.09 = 0.71
        assert explorador.temperature < 0.8

    def test_explorador_temp_clamped_min(self):
        gens = [GeneratorConfig(role=GeneratorRole.EXPLORADOR, model="m", temperature=0.4, prompt_template="t")]
        result = modulate_generators(gens, {"exploracion": 0.0})
        assert result[0].temperature >= 0.5

    def test_explorador_temp_clamped_max(self):
        gens = [GeneratorConfig(role=GeneratorRole.EXPLORADOR, model="m", temperature=1.1, prompt_template="t")]
        result = modulate_generators(gens, {"exploracion": 1.0})
        assert result[0].temperature <= 1.2

    def test_pragmatico_temp_unchanged(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"exploracion": 0.9})
        pragmatico = next(g for g in result if g.role == GeneratorRole.PRAGMATICO)
        assert pragmatico.temperature == 0.3


class TestConformidadPermission:
    def test_explorador_gets_permission_when_conformidad_low(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"conformidad": 0.2})
        explorador = next(g for g in result if g.role == GeneratorRole.EXPLORADOR)
        assert _EXTRA_PERMISSION_EXPLORADOR in explorador.prompt_template

    def test_explorador_no_permission_when_conformidad_normal(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"conformidad": 0.5})
        explorador = next(g for g in result if g.role == GeneratorRole.EXPLORADOR)
        assert _EXTRA_PERMISSION_EXPLORADOR not in explorador.prompt_template

    def test_pragmatico_never_gets_permission(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"conformidad": 0.1})
        pragmatico = next(g for g in result if g.role == GeneratorRole.PRAGMATICO)
        assert "cuestionar" not in pragmatico.prompt_template


class TestCautelaExtraProposals:
    def test_extra_pragmatico_when_cautela_high(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"cautela": 0.75})
        pragmatico_count = sum(1 for g in result if g.role == GeneratorRole.PRAGMATICO)
        assert pragmatico_count == 2

    def test_extra_pragmatico_has_higher_temp(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"cautela": 0.75})
        pragmaticos = [g for g in result if g.role == GeneratorRole.PRAGMATICO]
        assert pragmaticos[1].temperature > pragmaticos[0].temperature

    def test_extra_critico_when_cautela_very_high(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"cautela": 0.9})
        critico_count = sum(1 for g in result if g.role == GeneratorRole.CRITICO)
        assert critico_count == 2

    def test_no_extras_when_cautela_normal(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"cautela": 0.5})
        assert len(result) == 3

    def test_total_5_proposals_when_cautela_very_high(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"cautela": 0.9, "profundidad": 0.5})
        # 3 base + 1 extra pragmatico + 1 extra critico = 5
        assert len(result) == 5


class TestCombinedModulations:
    def test_profundidad_low_and_cautela_high(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"profundidad": 0.2, "cautela": 0.8})
        # critico removed, but extra pragmatico added
        roles = [g.role for g in result]
        assert GeneratorRole.CRITICO not in roles
        assert roles.count(GeneratorRole.PRAGMATICO) == 2

    def test_all_neutral_no_changes(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"cautela": 0.5, "exploracion": 0.5, "profundidad": 0.5, "conformidad": 0.5})
        assert len(result) == 3
        explorador = next(g for g in result if g.role == GeneratorRole.EXPLORADOR)
        assert explorador.temperature == 0.8  # unchanged

    def test_empty_posture_uses_defaults(self):
        gens = _default_generators()
        result = modulate_generators(gens, {})
        assert len(result) == 3


class TestDisciplinaModulation:
    def test_high_disciplina_adds_suffix_to_all(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"disciplina": 0.6})
        for g in result:
            assert _DISCIPLINA_HIGH_SUFFIX in g.prompt_template

    def test_normal_disciplina_no_suffix(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"disciplina": 0.5})
        for g in result:
            assert _DISCIPLINA_HIGH_SUFFIX not in g.prompt_template

    def test_low_disciplina_pragmatico_temp_increase(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"disciplina": 0.2})
        pragmatico = next(g for g in result if g.role == GeneratorRole.PRAGMATICO)
        assert pragmatico.temperature == 0.4  # 0.3 + 0.1

    def test_low_disciplina_explorador_temp_unchanged(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"disciplina": 0.2})
        explorador = next(g for g in result if g.role == GeneratorRole.EXPLORADOR)
        # explorador temp affected by exploracion (default 0.5), not disciplina
        assert explorador.temperature == 0.8

    def test_high_disciplina_combined_with_cautela(self):
        gens = _default_generators()
        result = modulate_generators(gens, {"disciplina": 0.6, "cautela": 0.8})
        # Should have suffix AND extra pragmatico
        assert all(_DISCIPLINA_HIGH_SUFFIX in g.prompt_template for g in result)
        pragmatico_count = sum(1 for g in result if g.role == GeneratorRole.PRAGMATICO)
        assert pragmatico_count == 2


class TestPhraseFromSnapshot:
    def test_empty_phrase_for_neutral_posture(self):
        phrase = phrase_from_snapshot({"cautela": 0.5, "exploracion": 0.5})
        assert phrase == ""

    def test_phrase_mentions_caution_when_high(self):
        phrase = phrase_from_snapshot({"cautela": 0.8})
        assert "reversibilidad" in phrase.lower() or "Priorizá" in phrase

    def test_phrase_mentions_risk_when_low(self):
        phrase = phrase_from_snapshot({"cautela": 0.2})
        assert "riesgo" in phrase.lower()

    def test_multiple_axes_combined(self):
        phrase = phrase_from_snapshot({"cautela": 0.8, "exploracion": 0.8})
        assert "Postura actual:" in phrase
        assert "reversibilidad" in phrase.lower() or "Priorizá" in phrase
        assert "alternativas" in phrase.lower()

    def test_empty_snapshot_returns_empty(self):
        assert phrase_from_snapshot({}) == ""
