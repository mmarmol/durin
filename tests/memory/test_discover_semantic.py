from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.extract_dream import _resolve_semantic_ref, discover_entities
from durin.memory.memory_writer import write_entity

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _stub(text):
    def inv(prompt, **kw):
        return text
    return inv


def _judge(verdict, conf):
    return (f"===VERDICT===\n{verdict}\n===CONFIDENCE===\n{conf}\n"
            f"===REASONING===\nstub\n===END===")


class _FakeVI:
    def __init__(self, rows):
        self._rows = rows

    def search(self, query, *, top_k=10, where=None):
        return self._rows[:top_k]


class _CrowdedIndex:
    """A same-type twin buried behind 50 unfiltered reference rows — only
    reachable when the caller asks the index for entity pages via `where`."""
    def __init__(self, twin_id, twin_type):
        self.twin = {"id": twin_id, "class_name": "entity_page", "_distance": 0.05,
                     "path": f"memory/entities/{twin_type}/{twin_id.split(':')[1]}.md"}
        self.noise = [{"id": f"reference:doc#{i}", "class_name": "reference", "_distance": 0.01 + i / 1000}
                      for i in range(50)]

    def search(self, query, *, top_k=10, where=None):
        rows = [self.twin] if where and "entity_page" in where else self.noise + [self.twin]
        return rows[:top_k]


def _page_path(ws, ref):
    t, _, s = ref.partition(":")
    return ws / "memory/entities" / t / f"{s}.md"


def _mk(ws, ref, name):
    write_entity(ws, ref, [FieldPatch(kind="attribute", key="city", value="NYC",
                 author="dream", source_ref="s", at=NOW)], create=True, name=name)


def _stub_for_discover_and_judge(discover_json, judge_text):
    calls = {"n": 0}
    def inv(prompt, **kw):
        calls["n"] += 1
        return discover_json if calls["n"] == 1 else judge_text
    return inv


def test_discover_semantic_updates_judged_same(tmp_path):
    # Existing "Bob Smith"; discovery proposes "Robert Smith" (no lexical match);
    # vector index says they're near; judge says same -> update Bob, no new page.
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    vi = _FakeVI([{"id": "person:bob_smith", "class_name": "entity_page", "_distance": 0.12}])
    out = discover_entities(
        tmp_path, "Robert Smith leads sales",
        vector_index=vi,
        llm_invoke=_stub_for_discover_and_judge(
            '[{"ref":"person:robert_smith","name":"Robert Smith",'
            '"attributes":{"role":"sales"}}]', _judge("same", 97)))
    assert out == [{"ref": "person:bob_smith", "committed": True}]
    assert not _page_path(tmp_path, "person:robert_smith").exists()
    page = EntityPage.from_file(_page_path(tmp_path, "person:bob_smith"))
    assert page.attributes["role"] == "sales"


def test_discover_semantic_creates_when_judged_different(tmp_path):
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    vi = _FakeVI([{"id": "person:bob_smith", "class_name": "entity_page", "_distance": 0.12}])
    out = discover_entities(
        tmp_path, "Robert Jones leads sales",
        vector_index=vi,
        llm_invoke=_stub_for_discover_and_judge(
            '[{"ref":"person:robert_jones","name":"Robert Jones",'
            '"attributes":{"role":"sales"}}]', _judge("different", 90)))
    assert out == [{"ref": "person:robert_jones", "committed": True}]
    assert _page_path(tmp_path, "person:robert_jones").exists()


def test_discover_semantic_skips_beyond_threshold(tmp_path):
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    vi = _FakeVI([{"id": "person:bob_smith", "class_name": "entity_page", "_distance": 0.55}])
    out = discover_entities(
        tmp_path, "Robert Jones",
        vector_index=vi, semantic_distance_threshold=0.20,
        llm_invoke=_stub('[{"ref":"person:robert_jones","name":"Robert Jones","attributes":{}}]'))
    # too far -> no judge, create new
    assert out == [{"ref": "person:robert_jones", "committed": True}]


def test_discover_semantic_finds_twin_past_crowded_window(tmp_path):
    # 50 unfiltered reference rows would push the same-type twin out of a
    # plain top-5 window; the entity-pages predicate must ask the index
    # directly instead of relying on a Python class_name/type filter.
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    vi = _CrowdedIndex("person:bob_smith", "person")
    out = discover_entities(
        tmp_path, "Robert Smith leads sales",
        vector_index=vi,
        llm_invoke=_stub_for_discover_and_judge(
            '[{"ref":"person:robert_smith","name":"Robert Smith",'
            '"attributes":{"role":"sales"}}]', _judge("same", 97)))
    assert out == [{"ref": "person:bob_smith", "committed": True}]


def test_resolve_semantic_ref_skips_a_ref_with_no_type(tmp_path):
    """A malformed ref with an empty type segment (e.g. ':slug') has no
    type to build the entity-pages predicate from — resolve to None
    without querying the index."""
    class _CountingVI:
        def __init__(self):
            self.calls = 0

        def search(self, query, *, top_k=10, where=None):
            self.calls += 1
            return []

    vi = _CountingVI()
    result = _resolve_semantic_ref(
        tmp_path, vi, ":noType", "Someone", {},
        llm_invoke=_stub("unused"), model=None,
        confidence_threshold=95, distance_threshold=0.30)
    assert result is None
    assert vi.calls == 0


def test_discover_no_vector_index_is_lexical_only(tmp_path):
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    out = discover_entities(
        tmp_path, "Robert Jones",
        llm_invoke=_stub('[{"ref":"person:robert_jones","name":"Robert Jones","attributes":{}}]'))
    assert out == [{"ref": "person:robert_jones", "committed": True}]
