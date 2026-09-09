from durin.memory.scope import ScopePredicate


def test_the_persons_memory_excludes_the_library_on_both_legs():
    p = ScopePredicate.for_search("all")
    assert p.vector_where == "class_name != 'reference'"
    assert p.fts_include is None
    assert p.fts_exclude == ("reference",)


def test_dreamed_and_undreamed_share_the_person_scope():
    assert ScopePredicate.for_search("dreamed") == ScopePredicate.for_search("all")
    assert ScopePredicate.for_search("undreamed") == ScopePredicate.for_search("all")


def test_library_is_only_the_library():
    p = ScopePredicate.for_search("library")
    assert p.vector_where == "class_name = 'reference'"
    assert p.fts_include == ("reference",)
    assert p.fts_exclude is None


def test_skills_narrow_the_person_scope_to_skill_rows():
    p = ScopePredicate.for_search("all", kinds="skill")
    assert p.vector_where == "class_name = 'skill'"
    assert p.fts_include == ("skill",)


def test_facts_keep_the_person_scope_without_skills():
    p = ScopePredicate.for_search("all", kinds="fact")
    assert p.vector_where == "class_name NOT IN ('reference', 'skill')"
    assert p.fts_exclude == ("reference", "skill")


def test_entity_pages_of_one_type():
    p = ScopePredicate.entity_pages("person")
    assert p.vector_where == "class_name = 'entity_page' AND id LIKE 'person:%'"
    assert p.fts_include == ("entity",)


def test_entity_pages_of_any_type():
    p = ScopePredicate.entity_pages()
    assert p.vector_where == "class_name = 'entity_page'"


def test_archive_scope_has_no_index_predicate():
    assert ScopePredicate.for_search("archive") == ScopePredicate.none()


def test_entity_type_is_quoted_for_the_filter_language():
    p = ScopePredicate.entity_pages("o'type")
    assert p.vector_where == "class_name = 'entity_page' AND id LIKE 'o''type:%'"
