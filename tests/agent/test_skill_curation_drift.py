import json

from durin.agent import skill_curation as sc


class _DR:
    """Stand-in for skill_drift.DriftReport."""
    def __init__(self, name, action, upstream_md, qdir):
        self.name, self.action, self.upstream_md, self.qdir = name, action, upstream_md, qdir
        self.source = "github:o/r/" + name


def _ws_with_skill(tmp_path, name, body):
    (tmp_path / "skills" / name).mkdir(parents=True)
    md = tmp_path / "skills" / name / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  durin:\n    mode: auto\n"
        f"    provenance:\n      source: \"github:o/r/{name}\"\n---\n{body}",
        encoding="utf-8")
    return tmp_path


def test_allow_drift_feeds_upstream_to_prompt(tmp_path, monkeypatch):
    ws = _ws_with_skill(tmp_path, "git", "local body with local edit\n")
    seen = {}

    def judge(prompt):
        seen["prompt"] = prompt
        return json.dumps({"actions": []})

    qdir = tmp_path / "q"
    qdir.mkdir()

    def drift(workspace, name, *, allowlist=None):
        return _DR(name, "allow", "UPSTREAM IMPROVED BODY", qdir)

    sc.curate_catalog(ws, judge=judge, drift_check=drift, allowlist=[])
    # the upstream body for the allow-drift skill must appear in the judge prompt
    assert "UPSTREAM IMPROVED BODY" in seen["prompt"]
    # qdir cleaned up
    assert not qdir.exists()


def test_risky_drift_not_in_prompt(tmp_path, monkeypatch):
    ws = _ws_with_skill(tmp_path, "git", "local body\n")
    seen = {}

    def judge(prompt):
        seen["prompt"] = prompt
        return json.dumps({"actions": []})

    qdir = tmp_path / "q2"
    qdir.mkdir()

    def drift(workspace, name, *, allowlist=None):
        return _DR(name, "confirm", "RISKY UPSTREAM BODY", qdir)

    sc.curate_catalog(ws, judge=judge, drift_check=drift, allowlist=[])
    # a confirm/block upstream is NOT fed to the auto-evolve judge
    assert "RISKY UPSTREAM BODY" not in seen["prompt"]
    assert not qdir.exists()  # still cleaned up


def test_no_drift_check_is_unchanged(tmp_path):
    ws = _ws_with_skill(tmp_path, "git", "body\n")
    # default drift_check=None → behaves exactly as before (no upstream section)
    out = sc.curate_catalog(ws, judge=lambda p: json.dumps({"actions": []}))
    assert out["reviewed"] == 1
