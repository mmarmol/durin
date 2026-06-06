"""A4: always_on distillation pass — rank (drop contradictions) + fit a token
budget + mark always_on, never deleting an entity."""
from datetime import datetime, timezone

from durin.memory.always_on_dream import run_always_on_pass
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.principal import _render_pinned_block, list_always_on
from durin.utils.helpers import estimate_text_tokens

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _feedback(ws, ref, text, name):
    write_entity(ws, ref, [FieldPatch(kind="body_append", value=text, author="agent",
                 source_ref="s", at=NOW)], create=True, name=name)


def _tok(ws, ref):
    t, s = ref.split(":", 1)
    page = EntityPage.from_file(ws / "memory" / "entities" / t / f"{s}.md")
    return estimate_text_tokens(_render_pinned_block(page))


def test_always_on_fits_budget_and_marks_without_data_loss(tmp_path):
    _feedback(tmp_path, "practice:verify", "Verify live before claiming done.", "Verify")
    _feedback(tmp_path, "stance:rigor", "Prefer rigor over speed.", "Rigor")
    _feedback(tmp_path, "stance:terse", "Be terse.", "Terse")
    budget = _tok(tmp_path, "practice:verify") + _tok(tmp_path, "stance:rigor")  # top 2 fit
    ranking = "practice:verify\nstance:rigor\nstance:terse"
    out = run_always_on_pass(tmp_path, token_budget=budget, llm_invoke=lambda p, **k: ranking)
    assert set(list_always_on(tmp_path)) == {"practice:verify", "stance:rigor"}
    assert out["selected"] == 2 and out["pruned"] == 1
    assert (tmp_path / "memory/entities/stance/terse.md").exists()  # pruned, NOT deleted


def test_always_on_drops_contradiction(tmp_path):
    _feedback(tmp_path, "stance:terse", "Always be terse.", "Terse")
    _feedback(tmp_path, "stance:verbose", "Always be detailed and verbose.", "Verbose")
    # the judge keeps terse and DROPS verbose (contradiction) → returns only terse
    out = run_always_on_pass(tmp_path, token_budget=10_000,
                             llm_invoke=lambda p, **k: "stance:terse")
    assert set(list_always_on(tmp_path)) == {"stance:terse"}
    assert out["dropped"] == 1
    assert (tmp_path / "memory/entities/stance/verbose.md").exists()  # dropped, NOT deleted


def test_always_on_fallback_without_llm(tmp_path):
    _feedback(tmp_path, "stance:a", "Guidance A.", "A")
    out = run_always_on_pass(tmp_path, token_budget=10_000, llm_invoke=None)
    assert list_always_on(tmp_path) == ["stance:a"]
    assert out["selected"] == 1


def test_always_on_unmarks_when_over_budget(tmp_path):
    # an item previously always_on but now pushed out of budget gets unmarked
    # (no data loss — the entity stays, only the flag flips).
    _feedback(tmp_path, "stance:old", "Old guidance.", "Old")
    from durin.memory.principal import mark_always_on
    mark_always_on(tmp_path, "stance:old", True)
    assert "stance:old" in list_always_on(tmp_path)
    out = run_always_on_pass(tmp_path, token_budget=0, llm_invoke=None)  # budget 0 → nothing fits
    assert list_always_on(tmp_path) == []
    assert out["selected"] == 0
    assert (tmp_path / "memory/entities/stance/old.md").exists()
