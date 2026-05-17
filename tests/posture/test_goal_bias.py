"""Tests for goal-sensitive posture initialization (doc §3.4)."""

from __future__ import annotations

import pytest

from durin.posture.goal_bias import compute_goal_bias
from durin.posture.vector import AxisName


class TestComputeGoalBias:
    def test_deploy_triggers_caution(self):
        deltas = compute_goal_bias("deploy to production")
        assert deltas[AxisName.CAUTION] == pytest.approx(0.10)

    def test_production_triggers_caution(self):
        deltas = compute_goal_bias("push to production environment")
        assert AxisName.CAUTION in deltas

    def test_delete_triggers_caution(self):
        deltas = compute_goal_bias("delete the old records")
        assert AxisName.CAUTION in deltas

    def test_force_push_triggers_caution(self):
        deltas = compute_goal_bias("force push to the branch")
        assert AxisName.CAUTION in deltas

    def test_migration_triggers_caution(self):
        deltas = compute_goal_bias("run the database migration")
        assert AxisName.CAUTION in deltas

    def test_explore_triggers_exploration(self):
        deltas = compute_goal_bias("explore options for the cache")
        assert deltas[AxisName.EXPLORATION] == pytest.approx(0.10)

    def test_research_triggers_exploration(self):
        deltas = compute_goal_bias("research alternatives for auth")
        assert AxisName.EXPLORATION in deltas

    def test_brainstorm_triggers_exploration(self):
        deltas = compute_goal_bias("let's brainstorm solutions")
        assert AxisName.EXPLORATION in deltas

    def test_protocol_triggers_discipline(self):
        deltas = compute_goal_bias("follow the protocol for deployment")
        assert deltas[AxisName.DISCIPLINE] == pytest.approx(0.10)

    def test_checklist_triggers_discipline(self):
        deltas = compute_goal_bias("run through the checklist")
        assert AxisName.DISCIPLINE in deltas

    def test_compliance_triggers_discipline(self):
        deltas = compute_goal_bias("ensure compliance with regulations")
        assert AxisName.DISCIPLINE in deltas

    def test_neutral_text_no_deltas(self):
        deltas = compute_goal_bias("write a hello world function")
        assert deltas == {}

    def test_multiple_keywords_multiple_axes(self):
        deltas = compute_goal_bias("explore options for the deploy to production")
        assert AxisName.CAUTION in deltas
        assert AxisName.EXPLORATION in deltas

    def test_case_insensitive(self):
        deltas = compute_goal_bias("DEPLOY A PRODUCCIÓN")
        assert AxisName.CAUTION in deltas

    def test_empty_text_no_deltas(self):
        deltas = compute_goal_bias("")
        assert deltas == {}

    def test_all_three_axes_triggered(self):
        deltas = compute_goal_bias(
            "explore the protocol for deploy to production"
        )
        assert AxisName.CAUTION in deltas
        assert AxisName.EXPLORATION in deltas
        assert AxisName.DISCIPLINE in deltas
