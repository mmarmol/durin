"""Tests for the model-capabilities resolver."""

from __future__ import annotations

import pytest

from durin.providers.capabilities import (
    ModelCapabilities,
    _candidate_keys,
    _heuristic_capabilities,
    _load_capabilities_snapshot,
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


def test_candidate_keys_canonical_first_then_legacy_variants():
    """The consensus file is keyed by lowercased bare names, so the
    canonical form is what the lookup uses first. Legacy provider /
    bedrock-prefixed forms follow as compatibility fallbacks."""
    keys = _candidate_keys("Claude-Opus-4-5", "anthropic")
    # Canonical (lowercase bare) is the primary key.
    assert keys[0] == "claude-opus-4-5"
    # Legacy variants still in the list.
    assert "anthropic/Claude-Opus-4-5" in keys
    assert "anthropic.Claude-Opus-4-5" in keys


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


def _snapshot_model(prefix: str, **flags: bool) -> str:
    """A snapshot model named ``prefix…`` whose entry has ``flags``.

    Read from the vendored snapshot at test time: the weekly refresh adds and
    drops models, so a pinned id would turn a data refresh into a red build.
    Skips when this snapshot has no such model.
    """
    snap = _load_capabilities_snapshot()
    for key in sorted(snap):
        entry = snap[key]
        if key.startswith(prefix) and all(
            bool(entry.get(flag)) is value for flag, value in flags.items()
        ):
            return key
    pytest.skip(f"no {prefix}* model with {flags} in this snapshot")


def test_lookup_claude_opus_has_vision():
    model = _snapshot_model("claude-opus-", supports_vision=True, supports_function_calling=True)
    caps = get_model_capabilities(model, "anthropic")
    assert caps.source == "snapshot"
    assert caps.supports_vision is True
    assert caps.supports_function_calling is True
    assert caps.max_input_tokens and caps.max_input_tokens > 100_000


def test_lookup_gpt_has_vision():
    model = _snapshot_model("gpt-", supports_vision=True, supports_function_calling=True)
    caps = get_model_capabilities(model, "openai")
    assert caps.source == "snapshot"
    assert caps.supports_vision is True
    assert caps.supports_function_calling is True


def test_lookup_gemini_picks_up_full_multimodal_set():
    """A Gemini model publishing vision + audio + video input surfaces all
    three from the consensus snapshot."""
    model = _snapshot_model(
        "gemini-", supports_vision=True, supports_audio_input=True, supports_video_input=True,
    )
    caps = get_model_capabilities(model, "google")
    assert caps.source == "snapshot"
    assert caps.supports_vision is True
    assert caps.supports_audio_input is True
    assert caps.supports_video_input is True


def test_lookup_glm_primary_reports_no_vision():
    """The user's primary pain point: regular GLM models don't do vision.
    The consensus snapshot must confirm so callers can choose to wire
    a vision aux model."""
    model = _snapshot_model("glm-", supports_vision=False)
    caps = get_model_capabilities(model, "custom")
    assert caps.source == "snapshot"
    assert caps.supports_vision is False


def test_lookup_glm_vision_variant_reports_vision():
    """And a vision variant must report ``vision=True`` — that's the
    bridge target the user configures as ``aux_models.vision``."""
    model = _snapshot_model("glm-", supports_vision=True)
    caps = get_model_capabilities(model, "custom")
    assert caps.source == "snapshot"
    assert caps.supports_vision is True


def test_an_aggregators_false_audio_claim_stays_out_of_the_snapshot():
    """302ai once declared audio for glm-5v-turbo, a Zhipu model without it.
    Only first-party vendor publications feed the snapshot, so the honest
    answer survives while the model is in it."""
    if "glm-5v-turbo" not in _load_capabilities_snapshot():
        pytest.skip("glm-5v-turbo is not in this snapshot")
    assert get_model_capabilities("glm-5v-turbo", "custom").supports_audio_input is False


def test_aggregator_keys_resolve_via_underlying_model():
    """Routing-service slugs (kilo/, vercel/, 302ai/, fireworks_ai/, …)
    are filtered out of the snapshot as standalone entries, but the
    canonical-key fallback strips the prefix so the underlying model
    still resolves correctly. This means consumers don't need to
    pre-strip aggregator prefixes themselves."""
    model = _snapshot_model("glm-", supports_vision=True)
    caps = get_model_capabilities(f"kilo/z-ai/{model}", None)
    # Resolves via the canonical bare entry from the snapshot.
    assert caps.source == "snapshot"
    assert caps.supports_vision is True
    # And the snapshot itself does NOT have the aggregator slug as a
    # direct key — verify by querying the loader.
    snap = _load_capabilities_snapshot()
    assert f"kilo/z-ai/{model}" not in snap
    assert "kilo" not in snap  # not even the top-level prefix
    assert not any(key.startswith("kilo/") for key in snap)
    # But the canonical bare name IS there.
    assert model in snap


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
    assert caps.source == "snapshot"
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
