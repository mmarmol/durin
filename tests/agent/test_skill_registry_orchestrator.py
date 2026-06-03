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


async def test_dedupe_by_ref_and_round_robin():
    a = _Fake("a", [_h("github:o/r/x", "a"), _h("github:o/r/y", "a")])
    b = _Fake("b", [_h("github:o/r/x", "b"), _h("github:o/r/z", "b")])  # x is a dup
    out = await search_registries("q", adapters=[a, b], allowlist=[], limit=10)
    assert [h.ref for h in out] == ["github:o/r/x", "github:o/r/z", "github:o/r/y"]


async def test_failing_adapter_does_not_sink_others():
    a = _Fake("a", [], boom=True)
    b = _Fake("b", [_h("github:o/r/z", "b")])
    out = await search_registries("q", adapters=[a, b], allowlist=[], limit=10)
    assert [h.ref for h in out] == ["github:o/r/z"]


async def test_allowlisted_floats_to_front():
    a = _Fake("a", [_h("github:other/r/x", "a"), _h("github:acme/r/y", "a")])
    out = await search_registries("q", adapters=[a], allowlist=["github:acme/"], limit=10)
    assert out[0].ref == "github:acme/r/y"
