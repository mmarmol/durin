from datetime import datetime, timezone

from durin.memory.aliases_index import AliasIndex
from durin.memory.extract_dream import mine_learnings
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity


class _FakeResp:
    def __init__(self, text): self.text = text


def test_learnings_resolves_to_existing_feedback_by_name(tmp_path):
    ws = tmp_path / "ws"
    # An existing canonical feedback entity:
    write_entity(ws, "feedback:spanish-language",
                 [FieldPatch(kind="body_replace", value="User writes in Spanish.",
                             author="dream", source_ref="t", at=datetime.now(timezone.utc))],
                 create=True, name="Spanish replies")
    idx = AliasIndex(ws / "memory"); idx.build()

    # The LLM proposes the SAME fact under a NEW slug, with a DISTINCT body:
    proposal_body = "User strongly prefers replies in Spanish."
    raw = ('[{"ref":"feedback:spanish-communication",'
           '"name":"Spanish replies","body":"' + proposal_body + '"}]')
    out = mine_learnings(ws, "USER: respondé en español",
                         llm_invoke=lambda *a, **k: _FakeResp(raw),
                         alias_index=idx)

    # It must have updated the existing entity, NOT minted a second slug.
    assert (ws / "memory" / "entities" / "feedback" / "spanish-language.md").exists()
    assert not (ws / "memory" / "entities" / "feedback" / "spanish-communication.md").exists()
    # The DISTINCT proposal body must now appear in the existing entity's file,
    # proving the update-in-place happened (not a no-op).
    assert proposal_body in (ws / "memory" / "entities" / "feedback" / "spanish-language.md").read_text()


# ---------------------------------------------------------------------------
# A body is replaced only by an answer that was shown it
# ---------------------------------------------------------------------------


def _seed(ws, ref, name, body, author="dream"):
    write_entity(ws, ref,
                 [FieldPatch(kind="body_replace", value=body, author=author,
                             source_ref="t", at=datetime.now(timezone.utc))],
                 create=True, name=name)


def _body(ws, ref):
    from durin.memory.entity_page import EntityPage
    type_, _, slug = ref.partition(":")
    return EntityPage.from_file(ws / "memory" / "entities" / type_ / f"{slug}.md").body.strip()


def _crowd(ws, n=45):
    """More learnings than the sweep's prompt shows; ``zz-late`` sorts last."""
    for i in range(n):
        _seed(ws, f"feedback:rule-{i:02d}", f"Rule {i}", f"Standing rule number {i}.")
    _seed(ws, "feedback:zz-late", "Late rule",
          "Never publish a draft. Why: the client reads it. How: ask first.")


def test_a_learning_that_resolves_to_an_unshown_page_is_asked_again_with_its_body(tmp_path):
    ws = tmp_path / "ws"
    _crowd(ws)
    prompts: list[str] = []

    def llm(prompt, **kw):
        prompts.append(prompt)
        if len(prompts) == 1:
            # The first answer never saw zz-late's body and would overwrite it.
            return _FakeResp('[{"ref":"feedback:zz-late","name":"Late rule",'
                             '"body":"Do not publish drafts."}]')
        return _FakeResp('[{"ref":"feedback:zz-late","name":"Late rule",'
                         '"body":"Never publish a draft. Why: the client reads it. '
                         'How: ask first, also for edits."}]')

    out = mine_learnings(ws, "USER: don't publish drafts, edits included", llm_invoke=llm)

    assert len(prompts) == 2
    # The first prompt did not hold the page; the second holds it and nothing else.
    assert "the client reads it" not in prompts[0]
    assert "the client reads it" in prompts[1]
    assert "Standing rule number 0." not in prompts[1]
    # What was written is the answer that saw the body.
    assert _body(ws, "feedback:zz-late").endswith("also for edits.")
    assert [r["ref"] for r in out] == ["feedback:zz-late"]


def test_the_learnings_event_counts_the_pages_asked_about_again(tmp_path, monkeypatch):
    import durin.memory.extract_dream as extract_dream

    events = []
    monkeypatch.setattr(extract_dream, "_mine_emit_tool_event",
                        lambda name, data: events.append((name, data)))
    ws = tmp_path / "ws"
    _crowd(ws)
    answers = iter(['[{"ref":"feedback:zz-late","name":"Late rule","body":"Shorter."}]', "[]"])

    mine_learnings(ws, "USER: about drafts",
                   llm_invoke=lambda *a, **k: _FakeResp(next(answers)))

    data = next(d for n, d in events if n == "memory.dream.learnings")
    assert data["reasked"] == 1 and data["written"] == 0


def test_an_unshown_page_the_second_answer_leaves_out_keeps_its_body(tmp_path):
    ws = tmp_path / "ws"
    _crowd(ws)
    answers = iter(['[{"ref":"feedback:zz-late","name":"Late rule","body":"Shorter."}]', "[]"])

    out = mine_learnings(ws, "USER: about drafts",
                         llm_invoke=lambda *a, **k: _FakeResp(next(answers)))

    assert _body(ws, "feedback:zz-late").startswith("Never publish a draft.")
    assert out == []


def test_a_new_slug_resolving_by_name_to_an_unshown_page_is_not_written_blind(tmp_path):
    ws = tmp_path / "ws"
    _crowd(ws)
    calls: list[str] = []

    def llm(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return _FakeResp('[{"ref":"feedback:no-draft-publishing","name":"Late rule",'
                             '"body":"Overwrites."}]')
        return _FakeResp("[]")

    mine_learnings(ws, "USER: about drafts", llm_invoke=llm)

    assert len(calls) == 2
    assert _body(ws, "feedback:zz-late").startswith("Never publish a draft.")
    assert not (ws / "memory" / "entities" / "feedback" / "no-draft-publishing.md").exists()


def test_shown_pages_and_new_learnings_are_written_from_the_first_answer(tmp_path):
    ws = tmp_path / "ws"
    _seed(ws, "feedback:brevity", "Brevity", "Prefers short answers.")
    calls: list[str] = []

    def llm(prompt, **kw):
        calls.append(prompt)
        return _FakeResp('[{"ref":"feedback:brevity","name":"Brevity",'
                         '"body":"Prefers short answers, bullets over prose."},'
                         '{"ref":"practice:tdd","name":"TDD","body":"Tests first."}]')

    out = mine_learnings(ws, "USER: bullets please, and tests first", llm_invoke=llm)

    assert len(calls) == 1
    assert _body(ws, "feedback:brevity").endswith("bullets over prose.")
    assert _body(ws, "practice:tdd") == "Tests first."
    assert sorted(r["ref"] for r in out) == ["feedback:brevity", "practice:tdd"]
