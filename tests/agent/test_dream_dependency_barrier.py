"""The dream may not pull a skill out from under a workflow that names it.

`dream_fuse_skills` REMOVES its sources once merged, and `dream_restructure_skill`
rewrites a skill's body in place. Both refuse `manual` skills — an ownership
check — but nothing asked whether anything depended on the skill, so an `auto`
skill named by a workflow node could be fused away, leaving the node pointing at
a name that no longer resolves.
"""

import json

from durin.agent.skills_store import dream_create_skill, dream_fuse_skills, dream_restructure_skill

_BODY = "---\nname: {n}\ndescription: do a thing. use when asked.\n---\nDelegate to the workflow.\n"


def _skill(ws, name):
    out = dream_create_skill(ws, name, _BODY.format(n=name), "seeded for the test")
    assert out.get("ok"), out
    return out


def _workflow_using(ws, skill):
    d = ws / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "triage.json").write_text(json.dumps({
        "name": "triage", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "skills": [skill]}],
    }))


def test_fuse_refuses_to_remove_a_referenced_source(tmp_path):
    _skill(tmp_path, "alpha")
    _skill(tmp_path, "beta")
    _workflow_using(tmp_path, "alpha")

    out = dream_fuse_skills(
        tmp_path, target="merged", sources=["alpha", "beta"],
        content=_BODY.format(n="merged"), rationale="tidy up")

    assert out.get("error")
    assert "triage" in out["error"]
    assert out.get("dependents")
    # The source survives: a refused fuse must not half-apply.
    assert (tmp_path / "skills" / "alpha" / "SKILL.md").is_file()
    assert not (tmp_path / "skills" / "merged").exists()


def test_fuse_still_works_when_nothing_depends_on_the_sources(tmp_path):
    _skill(tmp_path, "alpha")
    _skill(tmp_path, "beta")

    out = dream_fuse_skills(
        tmp_path, target="merged", sources=["alpha", "beta"],
        content=_BODY.format(n="merged"), rationale="tidy up")

    assert out.get("ok"), out
    assert not (tmp_path / "skills" / "alpha").exists()


def test_restructure_refuses_a_referenced_skill(tmp_path):
    _skill(tmp_path, "alpha")
    _workflow_using(tmp_path, "alpha")

    out = dream_restructure_skill(
        tmp_path, "alpha", content=_BODY.format(n="alpha"), rationale="rewrite")

    assert out.get("error")
    assert "triage" in out["error"]
    assert out.get("dependents")


def test_restructure_still_works_when_unreferenced(tmp_path):
    _skill(tmp_path, "alpha")

    out = dream_restructure_skill(
        tmp_path, "alpha", content=_BODY.format(n="alpha") + "\nNew body.\n",
        rationale="rewrite")

    assert out.get("ok"), out


def test_curation_does_not_retire_a_referenced_skill(tmp_path):
    """`retire` deletes outright. The guard sits at the autonomous call site, so a
    user deleting their own skill from the CLI or the webui is unaffected."""
    from durin.agent.skill_curation import curate_catalog

    _skill(tmp_path, "obsolete")
    _workflow_using(tmp_path, "obsolete")

    res = curate_catalog(tmp_path, judge=lambda _prompt: (
        '{"actions": [{"type": "retire", "name": "obsolete", '
        '"rationale": "fully superseded"}]}'))

    assert res["applied"] == 0
    assert (tmp_path / "skills" / "obsolete" / "SKILL.md").is_file()


def test_the_refusal_names_the_dependents_for_a_suggestion(tmp_path):
    """The dream's work is not discarded — the caller gets enough to surface it."""
    _skill(tmp_path, "alpha")
    _workflow_using(tmp_path, "alpha")

    out = dream_restructure_skill(
        tmp_path, "alpha", content=_BODY.format(n="alpha"), rationale="rewrite")

    assert [(d["kind"], d["name"], d["via"]) for d in out["dependents"]] == [
        ("workflow", "triage", "skills")]
