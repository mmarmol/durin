from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.refine_dream import add_tombstone, is_tombstoned, run_refine

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _judge_stub(verdict, conf, counter=None):
    def inv(prompt, **kw):
        if counter is not None:
            counter["n"] += 1
        return (f"===VERDICT===\n{verdict}\n===CONFIDENCE===\n{conf}\n"
                f"===REASONING===\nstub reasoning\n===END===")
    return inv


def _two_dupes(ws):
    # two entities sharing the alias "mxHERO" -> a merge candidate
    write_entity(ws, "company:mxhero",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO Inc.")
    write_entity(ws, "company:mxhero_inc",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO Incorporated")


def test_tombstone_roundtrip(tmp_path):
    assert not is_tombstoned(tmp_path, "company:a", "company:b")
    add_tombstone(tmp_path, "company:b", "company:a")   # order-independent
    assert is_tombstoned(tmp_path, "company:a", "company:b")


def test_refine_merges_same(tmp_path):
    _two_dupes(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 96))
    assert out["merged"], out
    assert not (tmp_path / "memory/entities/company/mxhero_inc.md").exists()  # absorbed
    assert (tmp_path / "memory/entities/company/mxhero.md").exists()          # canonical


def test_refine_respects_tombstone(tmp_path):
    _two_dupes(tmp_path)
    add_tombstone(tmp_path, "company:mxhero", "company:mxhero_inc")
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99))
    assert not out["merged"]
    assert any(s["reason"] == "tombstoned" for s in out["skipped"])
    assert (tmp_path / "memory/entities/company/mxhero_inc.md").exists()      # NOT merged


def test_refine_keeps_different(tmp_path):
    _two_dupes(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("different", 90))
    assert not out["merged"]
    assert out["kept_separate"]
    assert (tmp_path / "memory/entities/company/mxhero_inc.md").exists()


def test_refine_skips_user_managed(tmp_path):
    _two_dupes(tmp_path)
    p = tmp_path / "memory/entities/company/mxhero.md"
    page = EntityPage.from_file(p)
    page.author = "user_authored"                      # user opted to manage it
    p.write_text(page.to_markdown(), encoding="utf-8")
    counter = {"n": 0}
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99, counter))
    assert any(s["reason"] == "user_managed" for s in out["skipped"])
    assert counter["n"] == 0                            # judge not reached
    assert not out["merged"]
