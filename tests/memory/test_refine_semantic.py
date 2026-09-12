from datetime import datetime, timezone

from durin.memory.absorption import EntityAbsorption
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.refine_dream import run_refine

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
DIM = 8


def _judge_stub(verdict, conf):
    def inv(prompt, **kw):
        return (f"===VERDICT===\n{verdict}\n===CONFIDENCE===\n{conf}\n"
                f"===REASONING===\nstub\n===END===")
    return inv


def _vec(axis: int, offset: float = 0.0, offset_axis: int | None = None) -> list[float]:
    """A unit vector on ``axis``; with ``offset`` on ``offset_axis`` its squared
    L2 distance to the plain axis vector is ``offset ** 2``."""
    v = [0.0] * DIM
    v[axis] = 1.0
    if offset_axis is not None:
        v[offset_axis] = offset
    return v


class _FakeVI:
    """The index surface the semantic walk uses: every stored entity vector in
    one read, plus passage embedding for pages the index lacks."""
    def __init__(self, vectors, embed=None):
        self.vectors = dict(vectors)
        self._embed = embed or {}
        self.embedded: list[str] = []
        self.deleted: list[str] = []
        self.upserted: list[str] = []

    def entity_page_vectors(self):
        return dict(self.vectors)

    def embed_passages(self, texts):
        out = []
        for text in texts:
            self.embedded.append(text)
            vec = next((v for key, v in self._embed.items() if key.lower() in text.lower()), None)
            out.append(vec if vec is not None else [0.0] * DIM)
        return out

    # absorb() keeps the index current after a merge (best-effort); the fake
    # records the calls so a merge stays warning-free and we can assert upkeep.
    def delete_by_id(self, ref):
        self.deleted.append(ref)

    def upsert_entity_page(self, **kw):
        self.upserted.append(kw.get("entity_ref"))


def _mk(ws, ref, name):
    write_entity(ws, ref, [FieldPatch(kind="attribute", key="k", value="v",
                 author="dream", source_ref="s", at=NOW)], create=True, name=name)


def test_refine_merges_semantic_pair_without_shared_alias(tmp_path):
    # Two entities, SAME type, NO shared alias (different names) -> alias overlap
    # finds nothing; the stored vectors are 0.09 apart -> the judge sees the
    # pair, says same -> merge.
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    _mk(tmp_path, "person:robert_smith", "Robert Smith")
    vi = _FakeVI({"person:bob_smith": _vec(0), "person:robert_smith": _vec(0, 0.3, 1)})
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 97), vector_index=vi)
    assert out["merged"], out
    assert vi.embedded == []  # nothing re-embedded: both pages were in the index
    remaining = [p for p in ("bob_smith", "robert_smith")
                 if (tmp_path / f"memory/entities/person/{p}.md").exists()]
    assert len(remaining) == 1


def test_refine_semantic_respects_distance_threshold(tmp_path):
    # A neighbour beyond the threshold is not even judged (0.40 > 0.20).
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    _mk(tmp_path, "person:robert_smith", "Robert Smith")
    vi = _FakeVI({"person:bob_smith": _vec(0), "person:robert_smith": _vec(0, 0.40 ** 0.5, 1)})
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99), vector_index=vi,
                     semantic_distance_threshold=0.20)
    assert not out["merged"]


def test_refine_semantic_skips_cross_type(tmp_path):
    # Identical vectors but DIFFERENT types -> never a candidate.
    _mk(tmp_path, "person:mercury", "Mercury")
    _mk(tmp_path, "place:mercury", "Mercury")
    vi = _FakeVI({"person:mercury": _vec(0), "place:mercury": _vec(0)})
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99), vector_index=vi)
    assert not out["merged"]
    assert EntityAbsorption(tmp_path).find_semantic_candidates(vi, distance_threshold=0.30) == []


def test_find_semantic_candidates_embeds_only_the_pages_the_index_lacks(tmp_path):
    # robert_smith was written after the last index pass: the walk embeds it
    # as a passage (composed page text) and still finds the pair.
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    _mk(tmp_path, "person:robert_smith", "Robert Smith")
    vi = _FakeVI({"person:bob_smith": _vec(0)}, embed={"robert smith": _vec(0, 0.2, 1)})
    out = EntityAbsorption(tmp_path).find_semantic_candidates(vi, distance_threshold=0.30)
    assert out and set(out[0].refs) == {"person:bob_smith", "person:robert_smith"}
    assert abs(out[0].distance - 0.04) < 1e-5
    assert len(vi.embedded) == 1 and "Robert Smith" in vi.embedded[0]


def test_find_semantic_candidates_keeps_top_k_closest_per_page(tmp_path):
    # A tight cluster of three pages and a hub 0.04 away from all of them: the
    # cluster pages' two closest are each other, so the hub only enters through
    # its own top_k=2 list — two hub pairs, although all three are within the
    # threshold. Cluster pairs are each 0.0004-0.0008 apart and all kept.
    _mk(tmp_path, "topic:hub", "Hub")
    vectors = {"topic:hub": _vec(0, 0.2, 3)}
    for i, axis in enumerate((None, 1, 2)):
        _mk(tmp_path, f"topic:c{i}", f"Cluster {i}")
        vectors[f"topic:c{i}"] = _vec(0) if axis is None else _vec(0, 0.02, axis)
    vi = _FakeVI(vectors)
    out = EntityAbsorption(tmp_path).find_semantic_candidates(vi, distance_threshold=0.30, top_k=2)
    hub_pairs = [c for c in out if "topic:hub" in c.refs]
    cluster_pairs = [c for c in out if "topic:hub" not in c.refs]
    assert len(hub_pairs) == 2 and all(abs(c.distance - 0.04) < 1e-3 for c in hub_pairs)
    assert len(cluster_pairs) == 3 and all(c.distance < 0.001 for c in cluster_pairs)
    assert vi.embedded == []


def test_find_semantic_candidates_is_best_effort_when_the_index_fails(tmp_path):
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    _mk(tmp_path, "person:robert_smith", "Robert Smith")

    class _Broken:
        def entity_page_vectors(self):
            raise RuntimeError("table gone")

    assert EntityAbsorption(tmp_path).find_semantic_candidates(_Broken(), distance_threshold=0.30) == []


def test_refine_no_vector_index_is_alias_only(tmp_path):
    # vector_index=None -> behaves exactly as before (no semantic recall).
    _mk(tmp_path, "person:bob_smith", "Bob Smith")
    _mk(tmp_path, "person:robert_smith", "Robert Smith")
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99), vector_index=None)
    assert not out["merged"]  # no shared alias, no semantic -> nothing to merge
