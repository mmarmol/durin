from durin.providers.models_dev import apply_nvidia_live_ids, build_provider_models


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


def _entry(mid: str, **over):
    e = {
        "id": mid,
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "supports_vision": False,
        "supports_audio_input": False,
        "supports_pdf_input": False,
        "supports_reasoning": False,
        "supports_function_calling": True,
    }
    e.update(over)
    return e


def test_nvidia_live_ids_are_ground_truth():
    # models.dev re-spells NVIDIA's separators (3_1↔3.1, v03↔v0.3): the live
    # spelling must win while the models.dev caps carry over. Models absent
    # from the live list are dropped; live models absent from models.dev
    # appear with unknown caps.
    entries = [
        _entry("mistralai/mistral-7b-instruct-v03", supports_reasoning=True),
        _entry("abacusai/dracarys-llama-3_1-70b-instruct", max_input_tokens=99),
        _entry("z-ai/glm-5.1"),  # gone from the live list → dropped
    ]
    live = [
        "mistralai/mistral-7b-instruct-v0.3",
        "abacusai/dracarys-llama-3.1-70b-instruct",
        "nvidia/nemotron-4-340b-instruct",  # live-only → bare entry
    ]
    out = apply_nvidia_live_ids(entries, live)

    assert [e["id"] for e in out] == sorted(live)
    by_id = {e["id"]: e for e in out}
    assert by_id["mistralai/mistral-7b-instruct-v0.3"]["supports_reasoning"] is True
    assert by_id["abacusai/dracarys-llama-3.1-70b-instruct"]["max_input_tokens"] == 99
    bare = by_id["nvidia/nemotron-4-340b-instruct"]
    assert bare["max_input_tokens"] is None
    assert bare["supports_function_calling"] is False


def test_nvidia_live_ids_dedupe_and_exact_match():
    out = apply_nvidia_live_ids(
        [_entry("meta/llama-3.1-8b-instruct", supports_vision=True)],
        ["meta/llama-3.1-8b-instruct", "meta/llama-3.1-8b-instruct"],
    )
    assert len(out) == 1
    assert out[0]["supports_vision"] is True
