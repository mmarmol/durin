from durin.providers.models_dev import build_provider_models


def test_build_maps_ids_extracts_caps_and_skips_unknown():
    data = {
        "zai-coding-plan": {"models": {"glm-5.2": {
            "id": "glm-5.2", "reasoning": True, "tool_call": True,
            "modalities": {"input": ["text", "image"], "output": ["text"]},
            "limit": {"context": 1_000_000, "output": 131_072}}}},
        "anthropic": {"models": {"claude-opus-4-5": {
            "id": "claude-opus-4-5", "reasoning": True, "tool_call": True,
            "modalities": {"input": ["text", "image", "pdf"]},
            "limit": {"context": 200_000, "output": 64_000}}}},
        "kilocode": {"models": {"x": {"id": "x"}}},  # aggregator: not a durin provider
    }
    names = {"zai_coding_plan", "anthropic", "zhipu"}
    out = build_provider_models(data, names)

    assert set(out) == {"zai_coding_plan", "anthropic"}  # kilocode dropped
    glm = next(e for e in out["zai_coding_plan"] if e["id"] == "glm-5.2")
    assert glm["supports_vision"] is True
    assert glm["supports_reasoning"] is True
    assert glm["supports_function_calling"] is True
    assert glm["max_input_tokens"] == 1_000_000
    assert glm["max_output_tokens"] == 131_072
    cl = out["anthropic"][0]
    assert cl["supports_pdf_input"] is True
    assert cl["supports_vision"] is True


def test_exact_name_match_without_map_entry():
    # A models.dev id identical to a durin field needs no map entry.
    data = {"groq": {"models": {"llama-3.3-70b": {
        "id": "llama-3.3-70b", "modalities": {"input": ["text"]},
        "limit": {"context": 128_000, "output": 32_768}}}}}
    out = build_provider_models(data, {"groq"})
    assert out["groq"][0]["id"] == "llama-3.3-70b"
    assert out["groq"][0]["supports_vision"] is False
