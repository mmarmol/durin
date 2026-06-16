import durin.agent.skill_resolve as R
from durin.agent.skill_resolve import resolve_candidates

# --- local sources -----------------------------------------------------------

def test_local_skill_dir_one_candidate(tmp_path):
    d = tmp_path / "foo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: foo\ndescription: a demo\n---\nx\n")
    r = resolve_candidates(str(d))
    assert [c.name for c in r.candidates] == ["foo"]
    assert r.candidates[0].kind == "local"
    assert r.candidates[0].detail == "a demo"
    assert not r.unresolved_reason


def test_local_skill_md_file_resolves_to_its_dir(tmp_path):
    d = tmp_path / "foo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\n")
    r = resolve_candidates(str(d / "SKILL.md"))
    assert [c.name for c in r.candidates] == ["foo"]


def test_local_dir_of_many_skills(tmp_path):
    for n in ("a", "b"):
        s = tmp_path / "skills" / n
        s.mkdir(parents=True)
        (s / "SKILL.md").write_text(f"---\nname: {n}\ndescription: d\n---\n")
    r = resolve_candidates(str(tmp_path / "skills"))
    assert {c.name for c in r.candidates} == {"a", "b"}
    assert all(c.kind == "local" for c in r.candidates)


def test_local_no_skill_is_unresolved(tmp_path):
    r = resolve_candidates(str(tmp_path / "nope"))
    assert not r.candidates and r.unresolved_reason


# --- direct URL sources ------------------------------------------------------

def test_direct_skill_md_url():
    r = resolve_candidates("https://example.com/x/SKILL.md")
    assert len(r.candidates) == 1
    assert r.candidates[0].kind == "https"
    assert r.candidates[0].ref == "https://example.com/x/SKILL.md"


def test_unrecognized_url_is_unresolved():
    r = resolve_candidates("https://example.com/some/page")
    assert not r.candidates and r.unresolved_reason


# --- github sources (network stubbed) ----------------------------------------

def _fake_gh(tree):
    def _go(url, *a, **k):
        if url.rstrip("/").endswith("/repos/o/r"):
            return {"default_branch": "main"}
        if "/git/trees/main" in url:
            return {"tree": tree}
        raise AssertionError(f"unexpected url: {url}")
    return _go


def test_github_repo_lists_skill_dirs(monkeypatch):
    monkeypatch.setattr(R, "_gh_get_json", _fake_gh([
        {"path": "skills/a/SKILL.md", "type": "blob"},
        {"path": "skills/a/scripts/run.sh", "type": "blob"},
        {"path": "skills/b/SKILL.md", "type": "blob"},
        {"path": "README.md", "type": "blob"},
    ]))
    r = resolve_candidates("github:o/r")
    assert {c.name for c in r.candidates} == {"a", "b"}
    assert all(c.kind == "github" for c in r.candidates)
    assert all(c.ref.startswith("github:o/r@main/") for c in r.candidates)
    assert not r.unresolved_reason


def test_github_subpath_filters_to_one(monkeypatch):
    monkeypatch.setattr(R, "_gh_get_json", _fake_gh([
        {"path": "skills/a/SKILL.md", "type": "blob"},
        {"path": "skills/b/SKILL.md", "type": "blob"},
    ]))
    r = resolve_candidates("github:o/r/skills/a")
    assert [c.name for c in r.candidates] == ["a"]
    assert r.candidates[0].ref == "github:o/r@main/skills/a"


def test_github_web_url_form(monkeypatch):
    monkeypatch.setattr(R, "_gh_get_json", _fake_gh([
        {"path": "SKILL.md", "type": "blob"},
    ]))
    r = resolve_candidates("https://github.com/o/r")
    assert len(r.candidates) == 1
    assert r.candidates[0].ref == "github:o/r@main/"


def test_github_no_skills_is_unresolved(monkeypatch):
    monkeypatch.setattr(R, "_gh_get_json", _fake_gh([
        {"path": "README.md", "type": "blob"},
    ]))
    r = resolve_candidates("github:o/r")
    assert not r.candidates and r.unresolved_reason


def test_github_subpath_name_match_fallback(monkeypatch):
    """A registry slug that isn't the repo path (e.g. a skills.sh skillId)
    resolves by matching the skill dir's last segment anywhere in the tree."""
    monkeypatch.setattr(R, "_gh_get_json", _fake_gh([
        {"path": "skills/.curated/pdf/SKILL.md", "type": "blob"},
        {"path": "skills/other/SKILL.md", "type": "blob"},
    ]))
    r = resolve_candidates("github:o/r/pdf")
    assert [c.ref for c in r.candidates] == ["github:o/r@main/skills/.curated/pdf"]
    assert not r.unresolved_reason


def test_github_exact_subpath_beats_name_match(monkeypatch):
    """An exact subpath dir wins; the name-match fallback does NOT also pull
    same-named dirs elsewhere."""
    monkeypatch.setattr(R, "_gh_get_json", _fake_gh([
        {"path": "pdf/SKILL.md", "type": "blob"},
        {"path": "skills/.curated/pdf/SKILL.md", "type": "blob"},
    ]))
    r = resolve_candidates("github:o/r/pdf")
    assert [c.ref for c in r.candidates] == ["github:o/r@main/pdf"]
