"""Tests for loops config section."""

from durin.config.schema import Config


def test_loops_config_defaults():
    cfg = Config()
    assert cfg.loops.keep_runs == 20
    assert cfg.loops.check_timeout_s == 60
