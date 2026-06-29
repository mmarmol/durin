from durin.config.schema import AutoAbsorbConfig


def test_semantic_distance_threshold_default_is_loosened():
    # Recall threshold must admit near-duplicate pairs (~cosine 0.85);
    # the LLM judge (confidence_threshold) is the precision gate.
    assert AutoAbsorbConfig().semantic_distance_threshold == 0.30
