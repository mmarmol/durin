from durin.memory.search import Result, search_skills


def test_result_kind_is_skill():
    r = Result(
        source="memory",
        uri="skills/git/SKILL.md",
        headline="git",
        snippet="",
        class_name="skill",
    )
    assert r.kind == "skill"


def test_result_kind_entity_and_fragment_unaffected():
    assert (
        Result(
            source="memory",
            uri="x",
            headline="",
            snippet="",
            class_name="entity_page",
        ).kind
        == "canonical"
    )
    assert (
        Result(
            source="memory",
            uri="x",
            headline="",
            snippet="",
            class_name="episodic",
        ).kind
        == "fragment"
    )


def test_search_skills_finds_by_description(tmp_path):
    d = tmp_path / "skills" / "git-helper"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: git-helper\ndescription: interactive rebase workflow\n---\n"
        "run git rebase -i\n",
        encoding="utf-8",
    )
    results = list(search_skills(tmp_path, "rebase"))
    assert any(
        r.class_name == "skill" and r.headline == "git-helper" for r in results
    )
