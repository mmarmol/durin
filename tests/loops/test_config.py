"""Tests for loops config section."""

from durin.config.schema import Config
from durin.telemetry.schema import EVENTS


def test_loops_config_defaults():
    cfg = Config()
    assert cfg.loops.keep_runs == 20
    assert cfg.loops.check_timeout_s == 60


def test_loops_telemetry_events_registered():
    assert {"loops.fired", "loops.run_finished", "loops.escalated"} <= EVENTS.keys()
