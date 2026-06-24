"""N7a: the refine pass passes each page's file mtime to the absorb judge so its
staleness reasoning ("observed years apart → probably not the same") works —
previously the judge's "File last modified" line always rendered "(unknown)"."""
from datetime import datetime, timezone
from types import SimpleNamespace

from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.refine_dream import run_refine

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def test_refine_passes_page_mtimes_to_judge(tmp_path, monkeypatch):
    for ref, name in (("company:a", "A Inc"), ("company:a2", "A Incorporated")):
        write_entity(tmp_path, ref, [FieldPatch(kind="alias", value="A",
                     author="agent", source_ref="s", at=NOW)], create=True, name=name)

    captured: dict = {}

    def fake_judge(canonical, absorbed, shared, **kw):
        captured.update(kw)
        return SimpleNamespace(verdict="different", confidence=10, reasoning="x")

    monkeypatch.setattr("durin.memory.refine_dream.judge_pair", fake_judge)
    run_refine(tmp_path, llm_invoke=lambda *a, **k: "")

    assert isinstance(captured.get("canonical_mtime"), datetime)
    assert isinstance(captured.get("absorbed_mtime"), datetime)
