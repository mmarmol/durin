import asyncio

from durin.security import skill_judge


def _skill(tmp_path):
    d = tmp_path / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8")
    return d


def test_judge_astream_streams_reasoning_and_parses(tmp_path):
    raw = "===SUMMARY===\nReviewed; clean.\n===VERDICT===\nsafe\n===FINDINGS===\nnone\n===END===\n"

    async def fake_astream(prompt, *, model, on_reasoning=None, on_content=None):
        for piece in ("look", "ing"):
            r = on_reasoning(piece)
            if hasattr(r, "__await__"):
                await r
        return raw

    seen = []

    async def go():
        return await skill_judge.judge_skill_astream(
            _skill(tmp_path), ainvoke_stream=fake_astream, model="m",
            on_reasoning=lambda s: seen.append(s),
        )

    out = asyncio.run(go())
    assert out.verdict == "safe"
    assert out.summary == "Reviewed; clean."
    assert "".join(seen) == "looking"
