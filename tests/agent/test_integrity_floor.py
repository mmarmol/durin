"""Tier-1 integrity floor: no whole-body author (webui full-save, fuse, or the
raw-bytes memory path) may persist a structurally-broken artifact. Bounded
interactive edits are unaffected. Synthetic fixtures only."""
import pytest

from durin.agent import skills_store as ss


def _mk_auto(ws, name):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\nmetadata:\n  durin:\n    mode: auto\n---\n# {name}\n\nbody.\n",
        encoding="utf-8")


def test_save_skill_file_rejects_empty_skill_md(tmp_path):
    ws = tmp_path / "ws"
    _mk_auto(ws, "demo")
    r = ss.save_skill_file(ws, "demo", "SKILL.md", "   \n", rationale="edit")
    assert "error" in r and "empty" in r["error"]


def test_save_skill_file_rejects_bodyless_no_description(tmp_path):
    # frontmatter with no description + no derivable body prose → rejected
    ws = tmp_path / "ws"
    _mk_auto(ws, "demo")
    r = ss.save_skill_file(ws, "demo", "SKILL.md", "---\nname: demo\n---\n", rationale="edit")
    assert "error" in r and "description" in r["error"]


def test_save_skill_file_accepts_valid_and_bundled_files(tmp_path):
    ws = tmp_path / "ws"
    _mk_auto(ws, "demo")
    ok = ss.save_skill_file(ws, "demo", "SKILL.md",
                            "---\nname: demo\ndescription: d\n---\n# Demo\n\nbody.\n", rationale="edit")
    assert ok.get("ok") is True
    # the floor is scoped to SKILL.md: a bundled script is not description-checked
    ok2 = ss.save_skill_file(ws, "demo", "scripts/run.py", "print('hi')\n", rationale="add script")
    assert ok2.get("ok") is True


def test_bounded_edit_unaffected(tmp_path):
    # apply_skill_edit (interactive skill_edit / curation evolve) never routes
    # through the floor and keeps working on a valid skill.
    ws = tmp_path / "ws"
    _mk_auto(ws, "demo")
    r = ss.apply_skill_edit(ws, "demo", old="body.", new="better body.", rationale="tweak")
    assert r.get("ok") is True


def test_save_skill_file_rejects_broken_frontmatter_yaml(tmp_path):
    # An unquoted ": " in the description breaks the whole YAML frontmatter;
    # the body still yields a derivable description, so only an explicit
    # frontmatter check catches it.
    ws = tmp_path / "ws"
    _mk_auto(ws, "demo")
    broken = ("---\nname: demo\ndescription: use when drafting a note: claims "
              "need exhibits.\n---\n# Demo\n\nbody prose here.\n")
    r = ss.save_skill_file(ws, "demo", "SKILL.md", broken, rationale="edit")
    assert "error" in r and "YAML" in r["error"]


def test_bounded_edit_rejects_frontmatter_break(tmp_path):
    # A bounded replace inside the description CAN break the YAML (unquoted
    # colon) — refuse it instead of persisting an unparseable frontmatter.
    ws = tmp_path / "ws"
    _mk_auto(ws, "demo")
    r = ss.apply_skill_edit(
        ws, "demo", old="description: demo skill",
        new="description: demo skill — use when drafting a note: claims need exhibits",
        rationale="tweak")
    assert "error" in r and "YAML" in r["error"]
    # nothing persisted
    text = (ws / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert "exhibits" not in text


def test_fuse_rejects_broken_merged_body(tmp_path):
    ws = tmp_path / "ws"
    _mk_auto(ws, "a")
    _mk_auto(ws, "b")
    r = ss.dream_fuse_skills(ws, target="c", content="   ", sources=["a", "b"], rationale="x")
    assert "error" in r
    assert not (ws / "skills" / "c").exists()


def test_write_files_cas_rejects_invalid_entity_page(tmp_path):
    from durin.memory.memory_writer import write_files_cas
    (tmp_path / "memory").mkdir()
    with pytest.raises(ValueError, match="structurally-invalid entity page"):
        write_files_cas(tmp_path, {"entities/person/x.md": b"not a valid page, no frontmatter"},
                        message="bad")


def test_write_files_cas_accepts_valid_entity_page(tmp_path):
    from durin.memory.memory_writer import write_files_cas
    good = b"---\ntype: person\nname: X\n---\n\nbody\n"
    sha = write_files_cas(tmp_path, {"entities/person/x.md": good}, message="ok")
    assert sha  # committed
