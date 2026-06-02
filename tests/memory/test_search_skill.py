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
    hit = next(
        r for r in results
        if r.class_name == "skill" and r.headline == "git-helper"
    )
    # B1 (2026-06-03): the grep arm emits the canonical `skill/<slug>`
    # FUSION uri so it fuses with vector + FTS for the same skill (it
    # used to emit `skills/<slug>/SKILL.md`, splitting the RRF score).
    assert hit.uri == "skill/git-helper"
    # entities keeps the same `skill/<slug>` ref.
    assert hit.entities == ("skill/git-helper",)


def test_search_skills_uri_normalises_to_display_path(tmp_path):
    """The `skill/<slug>` grep uri still resolves to the drillable
    `skills/<slug>/SKILL.md` display path at the result layer."""
    from durin.agent.tools.memory_search import _skill_uri_to_path

    d = tmp_path / "skills" / "git-helper"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: git-helper\ndescription: interactive rebase workflow\n---\n"
        "run git rebase -i\n",
        encoding="utf-8",
    )
    hit = next(
        r for r in search_skills(tmp_path, "rebase")
        if r.class_name == "skill"
    )
    assert _skill_uri_to_path(hit.uri) == "skills/git-helper/SKILL.md"
