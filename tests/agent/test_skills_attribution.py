from durin.agent.skills_store import Attribution, attribution_to_trailers


def test_attribution_to_trailers_emits_present_fields_only():
    assert attribution_to_trailers(None) == {}
    assert attribution_to_trailers(Attribution(actor="user")) == {"Actor": "user"}
    assert attribution_to_trailers(
        Attribution(actor="agent", session="s1", agent="claude-opus-4-8")
    ) == {"Actor": "agent", "Session": "s1", "Agent": "claude-opus-4-8"}


def test_attribution_drops_empty_strings():
    assert attribution_to_trailers(Attribution(actor="agent", session="", agent=None)) == {"Actor": "agent"}
