from durin.agent import skills_store as ss


def _mk_auto(ws, name):
    d = ws / "skills" / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\nmetadata:\n  durin:\n    mode: auto\n---\nbody {name}\n",
        encoding="utf-8")


def _mk_manual(ws, name):
    d = ws / "skills" / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\nmetadata:\n  durin:\n    mode: manual\n---\nbody {name}\n",
        encoding="utf-8")


def test_fuse_writes_c_removes_sources(tmp_path):
    ws = tmp_path / "ws"
    _mk_auto(ws, "git-a"); _mk_auto(ws, "git-b")
    res = ss.dream_fuse_skills(
        ws, target="git-flow", content="# Git flow\n\nmerged\n",
        sources=["git-a", "git-b"], rationale="overlap")
    assert res.get("ok") is True
    assert (ws / "skills" / "git-flow" / "SKILL.md").exists()
    assert not (ws / "skills" / "git-a").exists()
    assert not (ws / "skills" / "git-b").exists()


def test_fuse_refuses_manual_source(tmp_path):
    ws = tmp_path / "ws"
    _mk_auto(ws, "git-a"); _mk_manual(ws, "mine")
    res = ss.dream_fuse_skills(ws, target="c", content="x",
                               sources=["git-a", "mine"], rationale="r")
    assert "error" in res
    assert (ws / "skills" / "git-a").exists()  # nothing removed on refusal


def test_fuse_refuses_origin_default_manual_workspace_source(tmp_path):
    # A user workspace skill with NO explicit mode is manual by origin → must be refused.
    ws = tmp_path / "ws"
    _mk_auto(ws, "git-a")
    d = ws / "skills" / "user-thing"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: user-thing\n---\nplain\n", encoding="utf-8")
    res = ss.dream_fuse_skills(ws, target="c", content="x",
                               sources=["git-a", "user-thing"], rationale="r")
    assert "error" in res
    assert (ws / "skills" / "user-thing").exists()


def test_fuse_refuses_existing_target(tmp_path):
    ws = tmp_path / "ws"
    _mk_auto(ws, "git-a"); _mk_auto(ws, "taken")
    res = ss.dream_fuse_skills(ws, target="taken", content="x",
                               sources=["git-a"], rationale="r")
    assert "error" in res


def test_read_bundle_files_skips_build_junk_and_binaries(tmp_path):
    """Bundles travel as text. A .pyc left behind by running a bundled script
    used to raise UnicodeDecodeError out of whichever curation action touched the
    skill, killing the whole nightly pass over a build artifact."""
    d = tmp_path / "skills" / "tooling"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: tooling\n---\nbody\n", encoding="utf-8")
    scripts = d / "scripts"; scripts.mkdir()
    (scripts / "run.py").write_text("print('hi')\n", encoding="utf-8")
    cache = scripts / "__pycache__"; cache.mkdir()
    # Real python 3.12 pyc magic — the exact byte that broke a live restructure.
    (cache / "run.cpython-312.pyc").write_bytes(b"\xcb\x0d\x0d\x0a\x00\x00")
    (d / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    (d / ".DS_Store").write_bytes(b"\x00\x01junk")

    files = ss.read_bundle_files(d)

    assert files == {"scripts/run.py": "print('hi')\n"}


def test_fuse_carries_text_bundles_past_a_stray_pyc(tmp_path):
    ws = tmp_path / "ws"
    _mk_auto(ws, "git-a"); _mk_auto(ws, "git-b")
    scripts = ws / "skills" / "git-a" / "scripts"; scripts.mkdir(parents=True)
    (scripts / "helper.sh").write_text("echo hi\n", encoding="utf-8")
    cache = scripts / "__pycache__"; cache.mkdir()
    (cache / "helper.cpython-312.pyc").write_bytes(b"\xcb\x0d\x0d\x0a")

    res = ss.dream_fuse_skills(
        ws, target="git-flow", content="# Git flow\n\nmerged\n",
        sources=["git-a", "git-b"], rationale="overlap")

    assert res.get("ok") is True
    assert (ws / "skills" / "git-flow" / "scripts" / "helper.sh").is_file()
    assert not (ws / "skills" / "git-flow" / "scripts" / "__pycache__").exists()
