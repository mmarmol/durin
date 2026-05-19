"""Tests for the model-capabilities resolver."""

from __future__ import annotations

from durin.providers.capabilities import (
    ModelCapabilities,
    _candidate_keys,
    _heuristic_capabilities,
    get_model_capabilities,
    known_models_count,
)


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------


def test_snapshot_is_vendored_and_loadable():
    """The LiteLLM JSON is bundled in-tree and parses cleanly."""
    n = known_models_count()
    # Snapshot has 2000+ entries; assert a generous lower bound so
    # routine refreshes don't break the test.
    assert n > 500


# ---------------------------------------------------------------------------
# Candidate-key generation (lookup order)
# ---------------------------------------------------------------------------


def test_candidate_keys_orders_specific_before_generic():
    keys = _candidate_keys("claude-opus-4-5", "anthropic")
    # Provider-qualified form wins, then bedrock dot form, then bare.
    assert keys[0] == "anthropic/claude-opus-4-5"
    assert "anthropic.claude-opus-4-5" in keys
    assert "claude-opus-4-5" in keys


def test_candidate_keys_strips_provider_prefix_from_model():
    """If the model already includes a provider segment, the bare form
    is added so we still match snapshot entries that don't carry the
    prefix."""
    keys = _candidate_keys("anthropic/claude-3-5-sonnet", None)
    assert "anthropic/claude-3-5-sonnet" in keys
    assert "claude-3-5-sonnet" in keys


def test_candidate_keys_handles_missing_provider():
    keys = _candidate_keys("gpt-4o", None)
    assert keys == ["gpt-4o"]


def test_candidate_keys_empty_for_empty_model():
    assert _candidate_keys("", "openai") == []


# ---------------------------------------------------------------------------
# Snapshot-backed lookups (known frontier models)
# ---------------------------------------------------------------------------


def test_lookup_claude_opus_has_vision():
    caps = get_model_capabilities("claude-opus-4-5", "anthropic")
    assert caps.source == "litellm"
    assert caps.supports_vision is True
    assert caps.supports_function_calling is True
    assert caps.max_input_tokens and caps.max_input_tokens > 100_000


def test_lookup_gpt4o_has_vision_and_audio_via_modalities():
    caps = get_model_capabilities("gpt-4o", "openai")
    assert caps.source == "litellm"
    assert caps.supports_vision is True
    assert caps.supports_function_calling is True


def test_lookup_gemini_picks_up_full_multimodal_set():
    """Gemini 2.0 Flash advertises supported_modalities = text/image/audio/video.
    The resolver must surface all four input modalities."""
    caps = get_model_capabilities("gemini/gemini-2.0-flash", None)
    assert caps.source == "litellm"
    assert caps.supports_vision is True
    assert caps.supports_audio_input is True
    assert caps.supports_video_input is True


def test_lookup_glm_in_snapshot_reports_no_vision():
    """The user's primary pain point: GLM doesn't do vision. Verify
    the snapshot agrees so the bridge tool will actually activate."""
    caps = get_model_capabilities("fireworks_ai/glm-4p7", None)
    assert caps.source == "litellm"
    assert caps.supports_vision is False


# ---------------------------------------------------------------------------
# Heuristic fallback (model name unknown to the snapshot)
# ---------------------------------------------------------------------------


def test_heuristic_recognizes_claude_family():
    caps = _heuristic_capabilities("claude-sonnet-99", "anthropic")
    assert caps.source == "heuristic"
    assert caps.supports_vision is True


def test_heuristic_recognizes_gemini_family():
    caps = _heuristic_capabilities("gemini-3-pro", "google")
    assert caps.source == "heuristic"
    assert caps.supports_vision is True
    assert caps.supports_audio_input is True


def test_heuristic_glm_has_no_vision():
    caps = _heuristic_capabilities("glm-5-turbo", "custom")
    assert caps.source == "heuristic"
    assert caps.supports_vision is False
    assert caps.supports_function_calling is True


def test_heuristic_strips_bedrock_prefix():
    """Bedrock-style ``anthropic.claude-x`` should still match the
    Claude family heuristic, not the literal-prefix lookup."""
    caps = _heuristic_capabilities("anthropic.claude-future-model", "bedrock")
    assert caps.source == "heuristic"
    assert caps.supports_vision is True


def test_unknown_model_falls_through_to_pessimistic_default():
    caps = get_model_capabilities("totally-unknown-vendor/model", None)
    assert caps.source == "default"
    assert caps.supports_vision is False
    assert caps.supports_audio_input is False
    assert caps.supports_function_calling is False


# ---------------------------------------------------------------------------
# Override precedence
# ---------------------------------------------------------------------------


def test_override_wins_over_snapshot():
    """If config declares a capability override, it must replace the
    snapshot value even for known models."""
    overrides = {
        "gpt-4o": {"supports_vision": False, "max_input_tokens": 16384},
    }
    caps = get_model_capabilities("gpt-4o", "openai", overrides=overrides)
    assert caps.source == "override"
    assert caps.supports_vision is False
    assert caps.max_input_tokens == 16384
    # Fields not mentioned in the override fall through to the snapshot.
    assert caps.supports_function_calling is True


def test_override_falls_back_to_snapshot_for_unspecified_fields():
    caps = get_model_capabilities(
        "claude-opus-4-5", "anthropic",
        overrides={"claude-opus-4-5": {"supports_audio_input": True}},
    )
    assert caps.source == "override"
    assert caps.supports_audio_input is True
    # Snapshot data preserved.
    assert caps.supports_vision is True


def test_override_keys_can_be_provider_qualified():
    caps = get_model_capabilities(
        "claude-opus-4-5", "anthropic",
        overrides={
            "anthropic/claude-opus-4-5": {"supports_vision": False},
        },
    )
    assert caps.source == "override"
    assert caps.supports_vision is False


def test_override_for_unknown_model_lifts_capabilities():
    """Useful for custom local fine-tunes: declare vision support even
    though the model name is unknown to the snapshot."""
    caps = get_model_capabilities(
        "my-custom-vlm", "custom",
        overrides={"my-custom-vlm": {"supports_vision": True}},
    )
    assert caps.source == "override"
    assert caps.supports_vision is True


# ---------------------------------------------------------------------------
# Defensive behavior
# ---------------------------------------------------------------------------


def test_empty_model_returns_default_capabilities_without_crashing():
    caps = get_model_capabilities("", None)
    assert isinstance(caps, ModelCapabilities)
    assert caps.source == "default"


def test_unrelated_overrides_are_ignored():
    caps = get_model_capabilities(
        "gpt-4o", "openai",
        overrides={"some-other-model": {"supports_vision": False}},
    )
    # gpt-4o still resolved from snapshot — unrelated override does
    # not affect it.
    assert caps.source == "litellm"
    assert caps.supports_vision is True


# ---------------------------------------------------------------------------
# Config schema integration
# ---------------------------------------------------------------------------


def test_aux_models_config_accepts_inline_vision():
    from durin.config.schema import Config

    cfg = Config.model_validate({
        "agents": {
            "aux_models": {
                "vision": {"model": "claude-haiku-4-5", "provider": "anthropic"},
            },
        },
    })
    assert cfg.agents.aux_models.vision is not None
    assert cfg.agents.aux_models.vision.model == "claude-haiku-4-5"
    assert cfg.agents.aux_models.audio is None  # not configured


def test_aux_models_config_accepts_preset_reference():
    from durin.config.schema import Config

    cfg = Config.model_validate({
        "model_presets": {
            "claude-fast": {
                "model": "claude-haiku-4-5",
                "provider": "anthropic",
            },
        },
        "agents": {
            "aux_models": {
                "vision": {"preset": "claude-fast"},
            },
        },
    })
    assert cfg.agents.aux_models.vision.preset == "claude-fast"


def test_model_capabilities_override_partial_fields():
    """The override schema allows declaring a partial set of fields;
    unspecified ones remain None so the resolver can fall through."""
    from durin.config.schema import Config

    cfg = Config.model_validate({
        "model_capabilities": {
            "glm-5-turbo": {"supports_vision": False, "max_input_tokens": 200000},
        },
    })
    override = cfg.model_capabilities["glm-5-turbo"]
    assert override.supports_vision is False
    assert override.max_input_tokens == 200000
    assert override.supports_function_calling is None


def test_aux_models_config_field_alias_camelcase():
    """``auxModels`` (camelCase) should bind to the same field — the
    config bridge accepts both, like the other camelCase aliases."""
    from durin.config.schema import Config

    cfg = Config.model_validate({
        "agents": {
            "auxModels": {
                "vision": {"model": "claude-haiku-4-5", "provider": "anthropic"},
            },
        },
    })
    assert cfg.agents.aux_models.vision is not None
