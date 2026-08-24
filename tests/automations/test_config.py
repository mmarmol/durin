"""Tests for the automations config section (successor to loops; see
LoopsConfig — both stay live and functional until the automations cutover
removes the loops side). Legacy `loops.*` keys populate the matching
`automations.*` field (with a deprecation log) when automations doesn't set
it explicitly; `loops.check_timeout_s` has no automations equivalent and is
never migrated."""

import json
import logging

import pytest
from pydantic import ValidationError

from durin.config.loader import load_config
from durin.config.schema import AuxModelConfig, Config


def test_automations_config_defaults():
    cfg = Config()
    assert cfg.automations.keep_runs == 20
    assert cfg.automations.queue_ttl_s == 3600


def test_automations_config_explicit_values_are_used():
    cfg = Config.model_validate({"automations": {"keep_runs": 7, "queue_ttl_s": 120}})
    assert cfg.automations.keep_runs == 7
    assert cfg.automations.queue_ttl_s == 120


def test_automations_keep_runs_ge_bound_enforced():
    with pytest.raises(ValidationError):
        Config.model_validate({"automations": {"keep_runs": 0}})


def test_automations_queue_ttl_s_ge_bound_enforced():
    with pytest.raises(ValidationError):
        Config.model_validate({"automations": {"queue_ttl_s": 59}})


def test_legacy_loops_only_populates_automations_with_deprecation_log(caplog):
    with caplog.at_level(logging.WARNING, logger="durin.config.schema"):
        cfg = Config.model_validate({"loops": {"keep_runs": 5, "queue_ttl_s": 90}})

    assert cfg.automations.keep_runs == 5
    assert cfg.automations.queue_ttl_s == 90
    # Both configs stay live until the cutover — loops fields are untouched.
    assert cfg.loops.keep_runs == 5
    assert cfg.loops.queue_ttl_s == 90
    assert any("deprecat" in r.getMessage().lower() for r in caplog.records)


def test_legacy_loops_check_timeout_s_is_never_migrated():
    cfg = Config.model_validate({"loops": {"check_timeout_s": 10}})
    assert cfg.loops.check_timeout_s == 10
    assert not hasattr(cfg.automations, "check_timeout_s")


def test_explicit_automations_wins_over_legacy_loops_per_field():
    cfg = Config.model_validate({
        "loops": {"keep_runs": 5, "queue_ttl_s": 90},
        "automations": {"keep_runs": 30},
    })
    assert cfg.automations.keep_runs == 30  # explicit value wins
    assert cfg.automations.queue_ttl_s == 90  # not set explicitly -> migrated
    # loops stays untouched either way
    assert cfg.loops.keep_runs == 5
    assert cfg.loops.queue_ttl_s == 90


def test_no_migration_and_no_deprecation_log_when_neither_section_given(caplog):
    with caplog.at_level(logging.WARNING, logger="durin.config.schema"):
        cfg = Config()

    assert cfg.automations.keep_runs == 20
    assert cfg.automations.queue_ttl_s == 3600
    assert not any("deprecat" in r.getMessage().lower() for r in caplog.records)


def test_aux_models_automations_populated_from_legacy_loops(caplog):
    with caplog.at_level(logging.WARNING, logger="durin.config.schema"):
        cfg = Config.model_validate({
            "agents": {"aux_models": {"loops": {"model": "m1", "provider": "nvidia"}}}
        })

    assert cfg.agents.aux_models.automations == AuxModelConfig(model="m1", provider="nvidia")
    # loops stays set — not cleared.
    assert cfg.agents.aux_models.loops == AuxModelConfig(model="m1", provider="nvidia")
    assert any("deprecat" in r.getMessage().lower() for r in caplog.records)


def test_aux_models_automations_explicit_wins_over_legacy_loops():
    cfg = Config.model_validate({
        "agents": {"aux_models": {
            "loops": {"model": "legacy-model", "provider": "nvidia"},
            "automations": {"model": "new-model", "provider": "zhipu"},
        }}
    })
    assert cfg.agents.aux_models.automations.model == "new-model"
    assert cfg.agents.aux_models.automations.provider == "zhipu"
    # loops keeps its own (different) value, untouched.
    assert cfg.agents.aux_models.loops.model == "legacy-model"


def test_aux_models_automations_defaults_to_none_when_neither_set():
    cfg = Config()
    assert cfg.agents.aux_models.automations is None
    assert cfg.agents.aux_models.loops is None


def test_automations_round_trips_through_load_config(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"loops": {"keepRuns": 5}}), encoding="utf-8")

    cfg = load_config(config_path)

    assert cfg.automations.keep_runs == 5
    assert cfg.loops.keep_runs == 5
