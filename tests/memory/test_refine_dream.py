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


def test_refine_merge_preserves_relations_and_attributes(tmp_path):
    # the canonical's relations + attributes must survive the absorb — the new
    # model carries the entity graph + structured facts on the page, and
    # _merge_pages used to drop them (data loss / G1).
    write_entity(tmp_path, "company:mxhero",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent", source_ref="s", at=NOW),
                  FieldPatch(kind="relation", value={"to": "person:alex", "type": "founded_by"},
                             author="agent", source_ref="s", at=NOW),
                  FieldPatch(kind="attribute", key="hq", value="US",
                             author="dream", source_ref="s", at=NOW)],
                 create=True, name="mxHERO Inc.")
    write_entity(tmp_path, "company:mxhero_inc",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent", source_ref="s", at=NOW)],
                 create=True, name="mxHERO Incorporated")
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 98))
    assert out["merged"]
    page = EntityPage.from_file(tmp_path / "memory/entities/company/mxhero.md")
    assert {"to": "person:alex", "type": "founded_by"} in page.relations
    assert page.attributes.get("hq") == "US"


def _set_created(ws, ref, dt):
    t, _, s = ref.partition(":")
    p = ws / "memory/entities" / t / f"{s}.md"
    page = EntityPage.from_file(p)
    page.created_at = dt
    p.write_text(page.to_markdown(), encoding="utf-8")


def test_refine_skips_pair_created_this_run(tmp_path):
    # Run-scoped quarantine: a pair created at/after the run start is the run's
    # own fresh output and is never merged this run.
    _two_dupes(tmp_path)
    _set_created(tmp_path, "company:mxhero", datetime(2026, 6, 10, tzinfo=timezone.utc))
    _set_created(tmp_path, "company:mxhero_inc", datetime(2026, 6, 10, tzinfo=timezone.utc))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99),
                     run_started_at=datetime(2026, 6, 9, tzinfo=timezone.utc))
    assert not out["merged"]
    assert any(s["reason"] == "quarantine" for s in out["skipped"])
    assert (tmp_path / "memory/entities/company/mxhero_inc.md").exists()


def test_refine_merges_pair_predating_run(tmp_path):
    # Entities that existed before the run started are eligible immediately.
    _two_dupes(tmp_path)
    _set_created(tmp_path, "company:mxhero", datetime(2026, 6, 10, tzinfo=timezone.utc))
    _set_created(tmp_path, "company:mxhero_inc", datetime(2026, 6, 10, tzinfo=timezone.utc))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99),
                     run_started_at=datetime(2026, 6, 11, tzinfo=timezone.utc))
    assert out["merged"]
    assert not (tmp_path / "memory/entities/company/mxhero_inc.md").exists()


def test_refine_no_cutoff_merges(tmp_path):
    # run_started_at=None disables the quarantine (standalone refine).
    _two_dupes(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99), run_started_at=None)
    assert out["merged"]


# ---------------------------------------------------------------------------
# Tier-2 routing tests (Task 5)
# ---------------------------------------------------------------------------

def test_refine_tier2_escalates_unclear_and_merges(tmp_path, monkeypatch):
    """Tier-1 unclear@80 → escalate → same@97 → merged."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    from durin.memory.absorb_judge import JudgeResult
    monkeypatch.setattr(rd, "_escalate_judge",
                        lambda ws, a, b, **kw: JudgeResult("same", 97, "confirmed same"))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=70)
    assert out["merged"], out
    assert not (tmp_path / "memory/entities/company/mxhero_inc.md").exists()


def test_refine_tier2_escalates_unclear_and_keeps(tmp_path, monkeypatch):
    """Tier-1 unclear@80 → escalate → different@85 → kept."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    from durin.memory.absorb_judge import JudgeResult
    monkeypatch.setattr(rd, "_escalate_judge",
                        lambda ws, a, b, **kw: JudgeResult("different", 85, "not the same"))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=70)
    assert not out["merged"], out
    assert out["kept_separate"]


def test_refine_tier2_escalates_same_in_borderline_window(tmp_path, monkeypatch):
    """Tier-1 same@75 (in [floor, threshold)) → escalate → same@97 → merged."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    from durin.memory.absorb_judge import JudgeResult
    monkeypatch.setattr(rd, "_escalate_judge",
                        lambda ws, a, b, **kw: JudgeResult("same", 97, "confirmed"))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 75),
                     confidence_threshold=95, escalate_floor=70)
    assert out["merged"], out


def test_refine_tier2_no_escalation_when_floor_zero(tmp_path, monkeypatch):
    """escalate_floor=0 disables Tier-2; unclear@80 → kept (old behavior)."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    called = {"n": 0}

    def _fake_escalate(ws, a, b, **kw):
        called["n"] += 1
        from durin.memory.absorb_judge import JudgeResult
        return JudgeResult("same", 97, "should not be called")

    monkeypatch.setattr(rd, "_escalate_judge", _fake_escalate)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=0)
    assert called["n"] == 0
    assert not out["merged"]
    assert out["kept_separate"]


def test_refine_tier2_no_escalation_same_above_threshold(tmp_path, monkeypatch):
    """same@97 >= threshold → merges directly, no escalation."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    called = {"n": 0}

    def _fake_escalate(ws, a, b, **kw):
        called["n"] += 1
        from durin.memory.absorb_judge import JudgeResult
        return JudgeResult("same", 99, "should not be called")

    monkeypatch.setattr(rd, "_escalate_judge", _fake_escalate)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 97),
                     confidence_threshold=95, escalate_floor=70)
    assert called["n"] == 0
    assert out["merged"]


def test_refine_tier2_best_effort_on_error(tmp_path, monkeypatch):
    """Tier-2 exception → pair kept (best-effort; never aborts pass)."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)

    def _raise(ws, a, b, **kw):
        raise RuntimeError("agent timeout")

    monkeypatch.setattr(rd, "_escalate_judge", _raise)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=70)
    assert not out["merged"]
    assert any("tier2_error" in str(k.get("reason", "")) for k in out["kept_separate"])
