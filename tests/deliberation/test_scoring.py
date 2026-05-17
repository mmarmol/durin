"""Tests for deliberation scoring functions."""

from __future__ import annotations

import pytest

from durin.deliberation.scoring import (
    compute_final_score,
    compute_threshold,
    compute_weight_avance,
    compute_weight_reversibilidad,
)


class TestWeightComputation:
    def test_neutral_cautela_equal_weights(self):
        assert compute_weight_avance(0.5) == pytest.approx(0.5)
        assert compute_weight_reversibilidad(0.5) == pytest.approx(0.5)

    def test_high_cautela_favors_reversibilidad(self):
        w_a = compute_weight_avance(0.9)
        w_r = compute_weight_reversibilidad(0.9)
        assert w_r > w_a
        assert w_a == pytest.approx(0.34)
        assert w_r == pytest.approx(0.66)

    def test_low_cautela_favors_avance(self):
        w_a = compute_weight_avance(0.1)
        w_r = compute_weight_reversibilidad(0.1)
        assert w_a > w_r
        assert w_a == pytest.approx(0.66)
        assert w_r == pytest.approx(0.34)

    def test_weights_sum_to_one(self):
        for cautela in [0.0, 0.25, 0.5, 0.75, 1.0]:
            w_a = compute_weight_avance(cautela)
            w_r = compute_weight_reversibilidad(cautela)
            assert w_a + w_r == pytest.approx(1.0)

    def test_extreme_cautela_zero(self):
        w_a = compute_weight_avance(0.0)
        w_r = compute_weight_reversibilidad(0.0)
        assert w_a == pytest.approx(0.7)
        assert w_r == pytest.approx(0.3)

    def test_extreme_cautela_one(self):
        w_a = compute_weight_avance(1.0)
        w_r = compute_weight_reversibilidad(1.0)
        assert w_a == pytest.approx(0.3)
        assert w_r == pytest.approx(0.7)


class TestFinalScore:
    def test_equal_scores_equal_weights(self):
        score = compute_final_score(avance=0.8, reversibilidad=0.8, cautela=0.5)
        assert score == pytest.approx(0.8)

    def test_high_cautela_weights_reversibilidad(self):
        score_safe = compute_final_score(avance=0.3, reversibilidad=0.9, cautela=0.9)
        score_risky = compute_final_score(avance=0.9, reversibilidad=0.3, cautela=0.9)
        assert score_safe > score_risky

    def test_low_cautela_weights_avance(self):
        score_progress = compute_final_score(avance=0.9, reversibilidad=0.3, cautela=0.1)
        score_safe = compute_final_score(avance=0.3, reversibilidad=0.9, cautela=0.1)
        assert score_progress > score_safe

    def test_zero_scores(self):
        score = compute_final_score(avance=0.0, reversibilidad=0.0, cautela=0.5)
        assert score == pytest.approx(0.0)

    def test_max_scores(self):
        score = compute_final_score(avance=1.0, reversibilidad=1.0, cautela=0.5)
        assert score == pytest.approx(1.0)

    def test_score_bounded_zero_to_one(self):
        for cautela in [0.0, 0.5, 1.0]:
            for avance in [0.0, 0.5, 1.0]:
                for rev in [0.0, 0.5, 1.0]:
                    s = compute_final_score(avance, rev, cautela)
                    assert 0.0 <= s <= 1.0


class TestThreshold:
    def test_neutral_profundidad(self):
        assert compute_threshold(0.5) == pytest.approx(0.55)

    def test_high_profundidad_raises_threshold(self):
        assert compute_threshold(0.9) == pytest.approx(0.67)

    def test_low_profundidad_lowers_threshold(self):
        assert compute_threshold(0.1) == pytest.approx(0.43)

    def test_zero_profundidad(self):
        assert compute_threshold(0.0) == pytest.approx(0.4)

    def test_max_profundidad(self):
        assert compute_threshold(1.0) == pytest.approx(0.7)

    def test_threshold_always_in_range(self):
        for p in [0.0, 0.25, 0.5, 0.75, 1.0]:
            t = compute_threshold(p)
            assert 0.4 <= t <= 0.7
