"""Tests for the Tier-2 sub-agent judge (Task 5)."""
import asyncio
import json
from datetime import datetime, timezone

from durin.agent.tools.memory_lineage_tools import MemorySourceDocumentTool
from durin.memory import pair_resolution, tier2_judge
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity


def test_guide_uses_the_relation_guidance_shared_with_tier1():
    """The merge-versus-relate rule and the relation-type guide must come
    from the shared constants, not a local copy — otherwise the cheap Tier-1
    judge (which injects the same constants into its own template) and this
    investigating judge could silently drift apart."""
    assert pair_resolution.MERGE_VS_RELATE_RULE in tier2_judge._GUIDE
    assert pair_resolution.RELATION_TYPE_GUIDE in tier2_judge._GUIDE


def test_escalate_judge_parses_agent_verdict(tmp_path, monkeypatch):
    class _FakeResult:
        final_content = ("===VERDICT===\nsame\n===CONFIDENCE===\n96\n"
                         "===REASONING===\nshared warning zone\n===END===")
        tool_events = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            names = set(spec.tools._tools.keys())
            assert {"memory_read_entity", "memory_entity_lineage",
                    "memory_source_session"} <= names
            return _FakeResult()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    j = tier2_judge.escalate_judge(tmp_path, "place:torrent", "place:torrent-valencia")
    assert j.verdict == "same" and j.confidence == 96


def test_escalate_judge_parses_different_verdict(tmp_path, monkeypatch):
    class _FakeResult:
        final_content = ("===VERDICT===\ndifferent\n===CONFIDENCE===\n85\n"
                         "===REASONING===\ndistinct homonyms\n===END===")
        tool_events = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            return _FakeResult()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    j = tier2_judge.escalate_judge(tmp_path, "person:a", "person:b")
    assert j.verdict == "different" and j.confidence == 85


# ---------------------------------------------------------------------------
# The reserved final-answer step
# ---------------------------------------------------------------------------

_ENVELOPE = ("===VERDICT===\nsame\n===CONFIDENCE===\n84\n"
             "===REASONING===\nsame slug lineage\n===END===")

_MAX_ITER_PLACEHOLDER = ("I reached the maximum number of tool call iterations (6) "
                         "without completing the task.")


def _investigation_messages():
    """What the runner hands back when the agent spent every iteration on tools."""
    return [
        {"role": "user", "content": "Decide whether these two memory entities..."},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "memory_read_entity",
                                      "arguments": '{"ref": "artifact:auth-page"}'}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "artifact:auth-page — the login page served by the auth SPA"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "type": "function",
                         "function": {"name": "memory_entity_lineage",
                                      "arguments": '{"ref": "artifact:auth-spa"}'}}]},
        {"role": "tool", "tool_call_id": "c2",
         "content": "artifact:auth-spa — created 2026-07-17 from session slack_C0A"},
        {"role": "assistant", "content": _MAX_ITER_PLACEHOLDER},
    ]


class _ExhaustedResult:
    final_content = _MAX_ITER_PLACEHOLDER
    stop_reason = "max_iterations"
    messages = _investigation_messages()
    tool_events = []


def test_escalate_judge_takes_a_final_answer_step_when_the_agent_runs_out_of_iterations(
        tmp_path, monkeypatch):
    """Live, every escalation of a pair whose investigation needed more than six
    tool iterations ended as `missing ===VERDICT=== block`: the runner's
    max-iterations text has no envelope. The judge must then spend ONE more
    call with no tools, handing the model its own investigation notes, and
    read the envelope from that."""
    specs = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            specs.append(spec)
            if len(specs) == 1:
                return _ExhaustedResult()

            class _Final:
                final_content = _ENVELOPE
                stop_reason = "completed"
                messages = []
                tool_events = []
            return _Final()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    j = tier2_judge.escalate_judge(tmp_path, "artifact:auth-page", "artifact:auth-spa")
    assert j.verdict == "same" and j.confidence == 84
    assert len(specs) == 2
    final = specs[1]
    assert final.tools._tools == {}, "the final step must not offer tools"
    assert final.max_iterations == 1
    assert len(final.initial_messages) == 1 and final.initial_messages[0]["role"] == "user"
    brief = final.initial_messages[0]["content"]
    assert "===VERDICT===" in brief and "===END===" in brief
    assert "artifact:auth-page" in brief and "artifact:auth-spa" in brief
    # The notes carry what the agent read, not only what it asked for.
    assert "login page served by the auth SPA" in brief
    assert "created 2026-07-17" in brief
    assert _MAX_ITER_PLACEHOLDER not in brief


def test_escalate_judge_raises_when_the_final_answer_still_lacks_the_envelope(
        tmp_path, monkeypatch):
    import pytest

    from durin.memory.absorb_judge import JudgeError
    runs = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            runs.append(spec)

            class _R:
                final_content = ("I think they are probably the same thing"
                                 if len(runs) == 2 else _MAX_ITER_PLACEHOLDER)
                stop_reason = "completed" if len(runs) == 2 else "max_iterations"
                messages = _investigation_messages()
                tool_events = []
            return _R()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    with pytest.raises(JudgeError):
        tier2_judge.escalate_judge(tmp_path, "person:a", "person:b")
    assert len(runs) == 2, "exactly one final-answer step, never a loop"


def test_an_unreadable_investigating_judge_reply_is_reported_to_telemetry(
        tmp_path, monkeypatch):
    """A failed Tier-2 answer sends the pair to a person. Each reply it could
    not read must reach the parse-failure event the first judge emits, with
    the raw head — two such pairs once reached Pending with no trace of what
    the model had written."""
    import pytest

    import durin.memory.llm_invoke as llm_invoke
    from durin.memory.absorb_judge import JudgeError
    seen = []
    monkeypatch.setattr(llm_invoke, "emit_parse_failure",
                        lambda stage, **kw: seen.append((stage, kw)))
    replies = ["===VERDICT===\nsame\n===CONFIDENCE===\nhigh\n===REASONING===\nx\n===END===",
               "I think they are probably the same thing"]

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            class _R:
                final_content = replies.pop(0)
                stop_reason = "completed"
                messages = []
                tool_events = []
            return _R()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    with pytest.raises(JudgeError):
        tier2_judge.escalate_judge(tmp_path, "person:a", "person:b")
    assert [stage for stage, _ in seen] == ["tier2_judge", "tier2_judge"]
    assert all(kw["source"] == "person:a|person:b" for _, kw in seen)
    # Why each was refused goes with it: a confidence that is no number, an
    # answer with no verdict envelope.
    assert all(kw.get("error") for _, kw in seen)
    assert seen[0][1]["raw"].startswith("===VERDICT===\nsame")
    assert seen[1][1]["raw"].startswith("I think they are")


def test_the_runners_out_of_iterations_text_is_no_unreadable_reply(tmp_path, monkeypatch):
    """Running out of tool iterations leaves the runner's own text, not an
    answer; the final step handles it, and telemetry stays for real replies."""
    import durin.memory.llm_invoke as llm_invoke
    seen = []
    monkeypatch.setattr(llm_invoke, "emit_parse_failure",
                        lambda stage, **kw: seen.append((stage, kw)))
    runs = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            runs.append(spec)
            if len(runs) == 1:
                return _ExhaustedResult()

            class _Final:
                final_content = _ENVELOPE
                stop_reason = "completed"
                messages = []
                tool_events = []
            return _Final()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    tier2_judge.escalate_judge(tmp_path, "artifact:auth-page", "artifact:auth-spa")
    assert seen == []


def test_escalate_judge_does_not_spend_a_final_step_when_the_agent_answered(
        tmp_path, monkeypatch):
    runs = []

    class _FakeRunner:
        def __init__(self, provider): pass

        async def run(self, spec):
            runs.append(spec)

            class _R:
                final_content = _ENVELOPE
                stop_reason = "completed"
                messages = []
                tool_events = []
            return _R()

    monkeypatch.setattr(tier2_judge, "AgentRunner", _FakeRunner)
    monkeypatch.setattr(tier2_judge, "_resolve_provider_model",
                        lambda: (object(), "fake-model"))
    tier2_judge.escalate_judge(tmp_path, "person:a", "person:b")
    assert len(runs) == 1


# ---------------------------------------------------------------------------
# Source-document evidence: the reference document a page was extracted from
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def _reference_doc(ws, slug, body, title="Spec"):
    d = ws / "memory" / "references"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(
        f"---\ntype: reference\ntitle: {title}\nsource: ''\n---\n\n{body}\n", encoding="utf-8")


def _stub_from(ws, ref, name, slugs, body="A one-line stub."):
    """A thin page extracted from reference documents, written the way the
    document seeding pass writes it."""
    patches = []
    for slug in slugs:
        src = f"[[references/{slug}.md]]"
        patches += [
            FieldPatch(kind="body_if_absent", value=body, author="dream", source_ref=src, at=_NOW),
            FieldPatch(kind="derived_from", value=f"reference:{slug}", author="dream",
                       source_ref=src, at=_NOW),
        ]
    write_entity(ws, ref, patches, create=True, name=name)


def _filler(tag, lines):
    return "\n".join(f"{tag} paragraph {i}: routine configuration notes." for i in range(lines))


def _read_documents(ws, ref):
    return asyncio.run(MemorySourceDocumentTool(ws).execute(ref=ref))


def test_source_document_returns_a_bounded_excerpt_around_the_entitys_mention(tmp_path):
    body = ("OPENING-SENTINEL overview of the statistics module.\n"
            + _filler("early", 300)
            + "\n### EmailError Structure\n"
              "The error payload embeds the original event as a nested field.\n"
            + _filler("late", 300) + "\nCLOSING-SENTINEL\n")
    _reference_doc(tmp_path, "stats-log-codes", body)
    _stub_from(tmp_path, "artifact:email-error-schema", "EmailError Structure", ["stats-log-codes"])

    out = _read_documents(tmp_path, "artifact:email-error-schema")

    (doc,) = out["documents"]
    assert doc["doc"] == "reference:stats-log-codes"
    assert doc["matched"] == ["EmailError Structure"]
    assert "embeds the original event as a nested field" in doc["excerpt"]
    assert "OPENING-SENTINEL" not in doc["excerpt"]
    assert "CLOSING-SENTINEL" not in doc["excerpt"]
    assert len(doc["excerpt"]) <= 3000 < len(body)


def test_source_document_shows_the_opening_when_the_page_name_is_absent(tmp_path):
    _reference_doc(tmp_path, "platform",
                   "OPENING-SENTINEL the platform overview.\n" + _filler("more", 300))
    _stub_from(tmp_path, "topic:inbox-abstraction", "Inbox Abstraction Layer", ["platform"])

    (doc,) = _read_documents(tmp_path, "topic:inbox-abstraction")["documents"]

    assert doc["matched"] == []
    assert doc["excerpt"].startswith("OPENING-SENTINEL")
    assert len(doc["excerpt"]) <= 3000


def test_source_document_fits_one_tool_result_for_a_page_citing_many_long_documents(tmp_path):
    """The investigating judge's runner cuts any tool result longer than its
    per-result ceiling (``max_tool_result_chars`` in ``tier2_judge``, 8000)
    at the head, which would drop every document after the first. However
    many documents a page cites, the result stays under it."""
    slugs = [f"doc-{i}" for i in range(6)]
    for s in slugs:
        _reference_doc(tmp_path, s, _filler(s, 50) + "\nStorage Providers upload attachments.\n"
                       + _filler(s, 600))
    _stub_from(tmp_path, "topic:storage-providers", "Storage Providers", slugs)

    out = _read_documents(tmp_path, "topic:storage-providers")

    assert len(json.dumps(out, ensure_ascii=False)) < 8000
    shown = out["documents"]
    assert shown and all("Storage Providers upload attachments" in d["excerpt"] for d in shown)
    assert len(shown) + out.get("documents_not_shown", 0) == len(slugs)


def test_source_document_refuses_a_document_outside_the_reference_library(tmp_path):
    ws = tmp_path / "ws"
    (ws / "memory" / "entities" / "artifact").mkdir(parents=True)
    (ws / "memory" / "references").mkdir(parents=True)
    (tmp_path / "secret.md").write_text("TOP-SECRET outside the workspace\n", encoding="utf-8")
    (ws / "secret.md").write_text("TOP-SECRET outside the library\n", encoding="utf-8")
    (ws / "memory" / "references" / "link.md").symlink_to(tmp_path / "secret.md")
    # A tampered page whose citations climb out of the reference library.
    (ws / "memory" / "entities" / "artifact" / "leaky.md").write_text(
        "---\ntype: artifact\nname: Leaky\naliases: []\n"
        "derived_from:\n- reference:../../../secret\n- reference:link\n"
        "provenance:\n  body:\n    source_ref: '[[references/../../secret.md]]'\n"
        "    author: dream\n    at: '2026-09-27T00:00:00+00:00'\n"
        "author: agent_created\n---\nLeaky page.\n", encoding="utf-8")

    out = _read_documents(ws, "artifact:leaky")

    assert len(out["documents"]) == 3
    assert all("error" in d and "excerpt" not in d for d in out["documents"])
    assert "TOP-SECRET" not in json.dumps(out)


def test_source_document_reports_a_missing_document_and_reads_the_rest(tmp_path):
    _reference_doc(tmp_path, "present", "Inbox Providers fetch mail from Gmail, Office365 and IMAP.")
    _stub_from(tmp_path, "topic:inbox-providers", "Inbox Providers", ["gone", "present"])

    gone, present = _read_documents(tmp_path, "topic:inbox-providers")["documents"]

    assert gone["doc"] == "reference:gone" and "error" in gone and "excerpt" not in gone
    assert "Gmail, Office365 and IMAP" in present["excerpt"]


def test_source_document_on_a_page_without_documents_and_on_a_missing_page(tmp_path):
    write_entity(tmp_path, "topic:chat-only",
                 [FieldPatch(kind="body_if_absent", value="Learned in a chat.", author="agent",
                             source_ref="[[sessions/s1.md#turn-1]]", at=_NOW)],
                 create=True, name="Chat only")

    assert _read_documents(tmp_path, "topic:chat-only")["documents"] == []
    assert "error" in _read_documents(tmp_path, "topic:nope")


def test_the_investigating_judge_is_offered_the_source_document_tool(tmp_path):
    assert "memory_source_document" in tier2_judge._build_tools(tmp_path).tool_names
