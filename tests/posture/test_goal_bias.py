"""Tests for goal-sensitive posture initialization (doc §3.4)."""

from __future__ import annotations

import pytest

from durin.posture.goal_bias import compute_goal_bias
from durin.posture.vector import AxisName


class TestComputeGoalBias:
    def test_deploy_triggers_cautela(self):
        deltas = compute_goal_bias("deploy a producción")
        assert deltas[AxisName.CAUTELA] == pytest.approx(0.10)

    def test_production_triggers_cautela(self):
        deltas = compute_goal_bias("push to production environment")
        assert AxisName.CAUTELA in deltas

    def test_delete_triggers_cautela(self):
        deltas = compute_goal_bias("delete the old records")
        assert AxisName.CAUTELA in deltas

    def test_force_push_triggers_cautela(self):
        deltas = compute_goal_bias("hago un force push al branch")
        assert AxisName.CAUTELA in deltas

    def test_migration_triggers_cautela(self):
        deltas = compute_goal_bias("run the database migration")
        assert AxisName.CAUTELA in deltas

    def test_explore_triggers_exploracion(self):
        deltas = compute_goal_bias("explorá opciones para el cache")
        assert deltas[AxisName.EXPLORACION] == pytest.approx(0.10)

    def test_research_triggers_exploracion(self):
        deltas = compute_goal_bias("research alternatives for auth")
        assert AxisName.EXPLORACION in deltas

    def test_brainstorm_triggers_exploracion(self):
        deltas = compute_goal_bias("let's brainstorm solutions")
        assert AxisName.EXPLORACION in deltas

    def test_protocolo_triggers_disciplina(self):
        deltas = compute_goal_bias("seguí el protocolo de deployment")
        assert deltas[AxisName.DISCIPLINA] == pytest.approx(0.10)

    def test_checklist_triggers_disciplina(self):
        deltas = compute_goal_bias("run through the checklist")
        assert AxisName.DISCIPLINA in deltas

    def test_compliance_triggers_disciplina(self):
        deltas = compute_goal_bias("ensure compliance with regulations")
        assert AxisName.DISCIPLINA in deltas

    def test_neutral_text_no_deltas(self):
        deltas = compute_goal_bias("write a hello world function")
        assert deltas == {}

    def test_multiple_keywords_multiple_axes(self):
        deltas = compute_goal_bias("explorá opciones para el deploy a producción")
        assert AxisName.CAUTELA in deltas
        assert AxisName.EXPLORACION in deltas

    def test_case_insensitive(self):
        deltas = compute_goal_bias("DEPLOY A PRODUCCIÓN")
        assert AxisName.CAUTELA in deltas

    def test_empty_text_no_deltas(self):
        deltas = compute_goal_bias("")
        assert deltas == {}

    def test_all_three_axes_triggered(self):
        deltas = compute_goal_bias(
            "explorá el protocolo de deploy a producción"
        )
        assert AxisName.CAUTELA in deltas
        assert AxisName.EXPLORACION in deltas
        assert AxisName.DISCIPLINA in deltas
