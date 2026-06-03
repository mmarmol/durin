from durin.agent.skill_lifecycle import sweep_unverified_skills
from durin.agent.skills_store import dream_create_skill
from durin.agent.skills_surface import quarantined_skills, skills_inventory


def _drop(parent, name, body="x\n"):
    """A manual / registry-CLI drop: a workspace skill with NO durin provenance."""
    d = parent / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}",
                                encoding="utf-8")


def test_dream_authored_skill_survives_sweep(tmp_path):
    # REGRESSION: a legitimately authored skill has provenance → the sweep keeps it.
    dream_create_skill(tmp_path, "authored",
                       "---\nname: authored\ndescription: d\n---\nbody\n",
                       rationale="test")
    assert sweep_unverified_skills(tmp_path) == []
    assert (tmp_path / "skills" / "authored").exists()


def test_manual_drop_is_swept(tmp_path):
    _drop(tmp_path, "rogue")
    assert sweep_unverified_skills(tmp_path) == ["rogue"]


def test_inventory_sweeps_then_excludes_unverified(tmp_path):
    _drop(tmp_path, "rogue")
    inv = skills_inventory(tmp_path)            # runs the sweep first
    assert "rogue" not in [s["name"] for s in inv]   # no longer active → inert


def test_quarantine_surface_shows_swept_skill(tmp_path):
    _drop(tmp_path, "rogue")
    q = quarantined_skills(tmp_path)            # runs the sweep first
    row = next((r for r in q if r["name"] == "rogue"), None)
    assert row is not None
    assert row["source"] == "unverified:workspace"
    assert any(f.get("category") == "unverified_origin" for f in row["findings"])


def test_context_builder_sweeps_on_init(tmp_path):
    from durin.agent.context import ContextBuilder
    _drop(tmp_path, "rogue")
    ContextBuilder(tmp_path)                    # __init__ runs the sweep
    assert not (tmp_path / "skills" / "rogue").exists()
    assert (tmp_path / ".durin" / "import-quarantine" / "rogue" / "SKILL.md").is_file()
