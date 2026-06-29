from pathlib import Path
from datetime import datetime, timezone
from durin.memory.extract_dream import mine_learnings
from durin.memory.memory_writer import write_entity
from durin.memory.field_patch import FieldPatch
from durin.memory.aliases_index import AliasIndex


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

    # The LLM proposes the SAME fact under a NEW slug:
    raw = ('[{"ref":"feedback:spanish-communication",'
           '"name":"Spanish replies","body":"User writes in Spanish."}]')
    out = mine_learnings(ws, "USER: respondé en español",
                         llm_invoke=lambda *a, **k: _FakeResp(raw),
                         alias_index=idx)

    # It must have updated the existing entity, NOT minted a second slug.
    assert (ws / "memory" / "entities" / "feedback" / "spanish-language.md").exists()
    assert not (ws / "memory" / "entities" / "feedback" / "spanish-communication.md").exists()
