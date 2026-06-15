"""Tests for fuzzy subsequence matching."""

from __future__ import annotations

from durin.cli.tui.fuzzy import fuzzy_match


def test_exact_match():
    assert fuzzy_match("glm", "glm-5.2") is True


def test_subsequence_match():
    assert fuzzy_match("g52", "glm-5.2") is True  # g...5...2


def test_case_insensitive():
    assert fuzzy_match("GLM", "glm-5.2") is True
    assert fuzzy_match("glm", "GLM-5.2") is True


def test_no_match():
    assert fuzzy_match("xyz", "glm-5.2") is False


def test_empty_query_matches_all():
    assert fuzzy_match("", "glm-5.2") is True


def test_empty_text_no_match():
    assert fuzzy_match("g", "") is False


def test_order_matters():
    assert fuzzy_match("25", "glm-5.2") is False  # 2 before 5 in query, 5 before 2 in text
    assert fuzzy_match("52", "glm-5.2") is True
