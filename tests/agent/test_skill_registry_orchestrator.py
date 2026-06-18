from durin.agent.skill_registry import SkillSearchHit, search_registries


class _Fake:
    def __init__(self, name, hits, *, boom=False):
        self.name, self._hits, self._boom = name, hits, boom
    async def search(self, query, *, limit):
        if self._boom:
            raise RuntimeError("down")
        return self._hits[:limit]


def _h(ref, reg):
    return SkillSearchHit(name=ref.split("/")[-1], ref=ref, registry=reg)


async def test_dedupe_by_ref_and_rank_fair():
    a = _Fake("a", [_h("github:o/r/x", "a"), _h("github:o/r/y", "a")])
    b = _Fake("b", [_h("github:o/r/x", "b"), _h("github:o/r/z", "b")])  # x is a dup
    out = await search_registries("q", adapters=[a, b], allowlist=[], limit=10)
    refs = [h.ref for h in out]
    assert refs.count("github:o/r/x") == 1  # deduped by ref (first adapter wins)
    assert set(refs) == {"github:o/r/x", "github:o/r/y", "github:o/r/z"}
    # rank-fair: both rank-1s (x, z) precede the lone rank-2 (y), whatever leads.
    assert set(refs[:2]) == {"github:o/r/x", "github:o/r/z"}
    assert refs[-1] == "github:o/r/y"


async def test_round_robin_leader_rotates_per_query():
    a = _Fake("a", [_h("a:1", "a"), _h("a:2", "a")])
    b = _Fake("b", [_h("b:1", "b"), _h("b:2", "b")])
    # deterministic for a given query
    o1 = await search_registries("foo", adapters=[a, b], allowlist=[], limit=10)
    o2 = await search_registries("foo", adapters=[a, b], allowlist=[], limit=10)
    assert [h.ref for h in o1] == [h.ref for h in o2]
    # across queries the top slot is NOT always the first adapter — the leader
    # rotates so neither registry permanently owns slot 0.
    leaders = set()
    for q in ["a", "b", "c", "d", "e", "f", "git", "pdf", "skill", "x", "y", "z"]:
        out = await search_registries(q, adapters=[a, b], allowlist=[], limit=10)
        leaders.add(out[0].registry)
    assert leaders == {"a", "b"}


async def test_failing_adapter_does_not_sink_others():
    a = _Fake("a", [], boom=True)
    b = _Fake("b", [_h("github:o/r/z", "b")])
    out = await search_registries("q", adapters=[a, b], allowlist=[], limit=10)
    assert [h.ref for h in out] == ["github:o/r/z"]


async def test_allowlisted_floats_to_front():
    a = _Fake("a", [_h("github:other/r/x", "a"), _h("github:acme/r/y", "a")])
    out = await search_registries("q", adapters=[a], allowlist=["github:acme/"], limit=10)
    assert out[0].ref == "github:acme/r/y"
