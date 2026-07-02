from durin.config.schema import AutoAbsorbConfig


def test_semantic_distance_threshold_default_is_loosened():
    # Recall threshold must admit near-duplicate pairs (~cosine 0.85);
    # the LLM judge (confidence_threshold) is the precision gate.
    assert AutoAbsorbConfig().semantic_distance_threshold == 0.30


def test_semantic_distance_signature_defaults_match_config():
    """The dream entry points must default to the config schema's value —
    a stale literal here means direct callers silently use an old threshold."""
    from inspect import signature
    from durin.config.schema import AutoAbsorbConfig
    from durin.memory import dream_passes, extract_dream, extract_runner, refine_dream

    cfg_default = AutoAbsorbConfig().semantic_distance_threshold
    for fn in (
        extract_dream.discover_entities,
        extract_dream.mine_learnings,
        extract_runner.run_extract_for_session,
        dream_passes.run_extract_pass,
        dream_passes.run_refine_pass,
        refine_dream.run_refine,
    ):
        default = signature(fn).parameters["semantic_distance_threshold"].default
        assert default == cfg_default, fn.__qualname__
