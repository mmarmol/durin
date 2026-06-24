from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.refine_dream import run_refine

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _judge_stub(verdict, conf):
    def inv(prompt, **kw):
        return (f"===VERDICT===\n{verdict}\n===CONFIDENCE===\n{conf}\n"
                f"===REASONING===\nstub\n===END===")
    return inv


class _FakeVI:
    """Returns controlled neighbors per query. `rows_by_substr` maps a substring
    of the query text to the rows search() should return for it."""
    def __init__(self, rows_by_substr):
        self._rows = rows_by_substr

    def search(self, query, *, top_k=10):
        for substr, rows in self._rows.items():
            if substr.lower() in query.lower():
                return rows[:top_k]
        return []


def _mk(ws, ref, name):
    write_entity(ws, ref, [FieldPatch(kind="attribute", key="k", value="v",
                 author="dream", source_ref="s", at=NOW)], create=True, name=name)


def test_refine_merges_semantic_pair_without_shared_alias(tmp_path):
    # Two entities, SAME type, NO shared alias (different names) -> alias overlap
    # finds nothing; semantic recall surfaces the pair; judge says same -> merge.
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    _mk(tmp_path, "person:robert_smith", "Robert Smith")
    vi = _FakeVI({
        "bob smith": [{"id": "person:robert_smith", "class_name": "entity_page", "_distance": 0.12}],
        "robert smith": [{"id": "person:bob_smith", "class_name": "entity_page", "_distance": 0.12}],
    })
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 97), vector_index=vi)
    assert out["merged"], out
    # one of the two was absorbed
    remaining = [p for p in ("bob_smith", "robert_smith")
                 if (tmp_path / f"memory/entities/person/{p}.md").exists()]
    assert len(remaining) == 1


def test_refine_semantic_respects_distance_threshold(tmp_path):
    # A neighbor BEYOND the threshold is not even judged.
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    _mk(tmp_path, "person:robert_smith", "Robert Smith")
    vi = _FakeVI({
        "bob smith": [{"id": "person:robert_smith", "class_name": "entity_page", "_distance": 0.40}],
        "robert smith": [{"id": "person:bob_smith", "class_name": "entity_page", "_distance": 0.40}],
    })
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99), vector_index=vi,
                     semantic_distance_threshold=0.20)
    assert not out["merged"]


def test_refine_semantic_skips_cross_type(tmp_path):
    # Same name-ish but DIFFERENT type -> not a candidate.
    _mk(tmp_path, "person:mercury", "Mercury")
    _mk(tmp_path, "place:mercury", "Mercury")
    vi = _FakeVI({
        "mercury": [
            {"id": "place:mercury", "class_name": "entity_page", "_distance": 0.05},
            {"id": "person:mercury", "class_name": "entity_page", "_distance": 0.05},
        ],
    })
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99), vector_index=vi)
    # cross-type pair filtered (in find_semantic_candidates and/or run_refine)
    assert not out["merged"]


def test_refine_no_vector_index_is_alias_only(tmp_path):
    # vector_index=None -> behaves exactly as before (no semantic recall).
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    _mk(tmp_path, "person:robert_smith", "Robert Smith")
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99), vector_index=None)
    assert not out["merged"]  # no shared alias, no semantic -> nothing to merge
