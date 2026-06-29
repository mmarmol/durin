from durin.memory.extract_dream import build_discover_prompt


def test_discover_prompt_includes_existing_manifest_and_reuse_instruction():
    p = build_discover_prompt("[turn-1] USER: ...",
                              existing="- topic:durin — durin: an agent project")
    assert "topic:durin" in p
    assert "EXISTING ENTITIES" in p
    assert "reuse" in p.lower()


def test_discover_prompt_without_existing_still_builds():
    p = build_discover_prompt("[turn-1] USER: hi")
    assert "EXISTING ENTITIES" in p
    assert "(none yet)" in p
    assert "JSON" in p
