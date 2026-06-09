from durin.agent import skills_store as ss
from durin.agent.skill_curation import curate_catalog


def _mk(ws, name, body="body"):
    d = ws / "skills" / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\nmetadata:\n  durin:\n    mode: auto\n"
        f"    provenance:\n      source: dream\n---\n{body}\n", encoding="utf-8")


def test_curate_reviews_only_the_changed_delta(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "changed", "new body")        # no cursor → in delta
    _mk(ws, "stable")
    ss.mark_curated(ws, "stable")          # body unchanged since → NOT in delta

    calls = []
    def fake_judge(prompt):
        calls.append(prompt); return '{"actions": []}'

    res = curate_catalog(ws, judge=fake_judge)
    assert res["reviewed"] == 1
    assert "changed" in calls[0] and "stable" not in calls[0]


def test_curate_fuses_when_judge_says_overlap(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "git rebase steps")
    _mk(ws, "git-b", "git rebase steps too")

    def fake_judge(prompt):
        return ('{"actions": [{"type": "fuse", "target": "git-flow", '
                '"sources": ["git-a", "git-b"], "content": "# Git flow\\nmerged\\n", '
                '"rationale": "same steps"}]}')

    res = curate_catalog(ws, judge=fake_judge)
    assert res["applied"] == 1
    assert (ws / "skills" / "git-flow" / "SKILL.md").exists()
    assert not (ws / "skills" / "git-a").exists()


def test_curate_budget_caps_delta_and_defers_rest(tmp_path):
    ws = tmp_path / "ws"
    for n in ("a", "b", "c"):
        _mk(ws, n)
    res = curate_catalog(ws, judge=lambda p: '{"actions": []}', budget=2)
    assert res["reviewed"] == 2
    assert res["deferred"] == 1


def test_curate_noop_on_empty_delta(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "solo")
    ss.mark_curated(ws, "solo")            # unchanged → empty delta
    calls = []
    res = curate_catalog(ws / ".", judge=lambda p: calls.append(p) or '{"actions": []}')
    # NOTE: ws has one stable skill → delta empty → judge never called
    assert res == {"reviewed": 0, "applied": 0, "deferred": 0, "observations": {"applied": 0, "declined": 0, "kept": 0}}
    assert calls == []


def test_curate_excludes_pristine_builtins(tmp_path):
    # No workspace skills created → with real shipped builtins present,
    # the delta must still be empty (builtins are source="builtin", excluded).
    ws = tmp_path / "ws"; ws.mkdir()
    calls = []
    res = curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert res == {"reviewed": 0, "applied": 0, "deferred": 0, "observations": {"applied": 0, "declined": 0, "kept": 0}}
    assert calls == []


def test_curate_skips_fuse_with_out_of_scope_source(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "body a")  # the only reviewed skill
    def judge(prompt):
        # judge names a source ("ghost") that was never in the catalog
        return ('{"actions": [{"type": "fuse", "target": "x", '
                '"sources": ["git-a", "ghost"], "content": "merged", "rationale": "r"}]}')
    res = curate_catalog(ws, judge=judge)
    assert res["applied"] == 0
    assert (ws / "skills" / "git-a").exists()   # not fused away
    assert not (ws / "skills" / "x").exists()


def test_curate_skips_evolve_of_out_of_scope_skill(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "body a")
    def judge(prompt):
        return ('{"actions": [{"type": "evolve", "name": "ghost", '
                '"old": "x", "new": "y", "rationale": "r"}]}')
    res = curate_catalog(ws, judge=judge)
    assert res["applied"] == 0
