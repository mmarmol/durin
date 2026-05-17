"""Tests for hook_factory — automatic hook wiring from config."""

from __future__ import annotations

import pytest

from durin.agent.hook_factory import build_hooks_from_config


def _make_config(posture_enabled=False, deliberation_enabled=False, provider="local"):
    """Build a minimal config-like object for testing."""
    from unittest.mock import MagicMock

    axis_cfg = MagicMock()
    axis_cfg.mean = 0.5
    axis_cfg.variance = 0.15
    axis_cfg.return_force = 0.3

    posture = MagicMock()
    posture.enabled = posture_enabled
    posture.axes = {
        "caution": axis_cfg,
        "exploration": axis_cfg,
        "depth": axis_cfg,
        "discipline": axis_cfg,
        "conformity": axis_cfg,
    }

    gen_cfg = MagicMock()
    gen_cfg.model = "test-model"
    gen_cfg.temperature = 0.5
    gen_cfg.max_tokens = 256
    gen_cfg.enabled = True

    eval_cfg = MagicMock()
    eval_cfg.model = "test-model"
    eval_cfg.max_tokens = 64
    eval_cfg.temperature = 0.0

    deliberation = MagicMock()
    deliberation.enabled = deliberation_enabled
    deliberation.provider = provider
    deliberation.max_rounds = 2
    deliberation.generators = {
        "pragmatic": gen_cfg,
        "explorer": gen_cfg,
        "critic": gen_cfg,
    }
    deliberation.evaluators = {
        "avance": eval_cfg,
        "reversibilidad": eval_cfg,
    }

    defaults = MagicMock(spec=[])
    defaults.posture = posture
    defaults.deliberation = deliberation

    config = MagicMock()
    config.agents.defaults = defaults
    return config


class TestBuildHooksFromConfig:
    def test_both_disabled_returns_empty(self):
        config = _make_config(posture_enabled=False, deliberation_enabled=False)
        hooks = build_hooks_from_config(config)
        assert hooks == []

    def test_posture_only(self):
        config = _make_config(posture_enabled=True, deliberation_enabled=False)
        hooks = build_hooks_from_config(config)
        assert len(hooks) == 1

        from durin.posture.hook import PostureHook
        assert isinstance(hooks[0], PostureHook)

    def test_posture_hook_has_correct_vector(self):
        config = _make_config(posture_enabled=True)
        hooks = build_hooks_from_config(config)

        from durin.posture.hook import PostureHook
        hook = hooks[0]
        assert isinstance(hook, PostureHook)
        snapshot = hook.current_vector.snapshot()
        assert len(snapshot) == 5

    def test_deliberation_with_unknown_provider_skipped(self):
        config = _make_config(posture_enabled=False, deliberation_enabled=True, provider="nonexistent")
        hooks = build_hooks_from_config(config)
        assert hooks == []

    def test_telemetry_created_with_session_key(self, tmp_path):
        config = _make_config(posture_enabled=True)
        hooks = build_hooks_from_config(config, session_key="test:session")
        assert len(hooks) == 1
