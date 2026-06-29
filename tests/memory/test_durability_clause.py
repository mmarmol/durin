from durin.memory.extract_dream import build_discover_prompt
from durin.utils.prompt_templates import render_template


def test_discover_prompt_excludes_third_party_content():
    p = build_discover_prompt("[turn-1] USER: ...")
    low = p.lower()
    assert "third-party" in low or "third party" in low
    assert "advertis" in low      # advertisement/advertising
    assert "transcri" in low      # transcription/transcribed


def test_learnings_prompt_excludes_third_party_content():
    p = render_template("agent/consolidator_learnings.md", existing="(none)")
    low = p.lower()
    assert "third-party" in low or "third party" in low
    assert "advertis" in low
