from durin.agent.skill_curation import curate_catalog


def test_curate_noop_when_no_workspace_skills(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    calls = []
    res = curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert res == {"reviewed": 0, "applied": 0, "deferred": 0, "observations": {"applied": 0, "declined": 0, "kept": 0, "open": 0}, "principles": 0}
    assert calls == []
