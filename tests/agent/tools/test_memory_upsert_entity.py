import asyncio

from durin.agent.tools.memory_upsert_entity import MemoryUpsertEntityTool
from durin.memory.entity_page import EntityPage


def _page(ws, ref):
    type_, _, slug = ref.partition(":")
    return EntityPage.from_file(ws / "memory" / "entities" / type_ / f"{slug}.md")


def test_tool_authors_entity(tmp_path):
    tool = MemoryUpsertEntityTool(workspace=str(tmp_path))
    out = asyncio.run(tool.execute(
        ref="company:mxhero", name="mxHERO Inc.",
        aliases=["mxHERO"],
        relations=[{"to": "company:carahsoft", "type": "partner"}],
        body="Won the Box 2025 Solution Partner award."))
    assert out == {"ref": "company:mxhero", "committed": True}
    page = _page(tmp_path, "company:mxhero")
    assert page.name == "mxHERO Inc."
    assert "mxHERO" in page.aliases
    assert any(r["to"] == "company:carahsoft" for r in page.relations)
    assert "Box 2025" in page.body
    # the field author is recorded as "agent" (resolved from author_scope)
    assert page.provenance["relations"][0]["author"] == "agent"


def test_tool_merges_existing(tmp_path):
    tool = MemoryUpsertEntityTool(workspace=str(tmp_path))
    asyncio.run(tool.execute(ref="company:x", name="X", body="first"))
    asyncio.run(tool.execute(
        ref="company:x", relations=[{"to": "topic:t", "type": "about"}]))
    page = _page(tmp_path, "company:x")
    assert page.name == "X"                       # preserved across merge
    assert any(r["to"] == "topic:t" for r in page.relations)


def test_tool_records_derived_from(tmp_path):
    tool = MemoryUpsertEntityTool(workspace=str(tmp_path))
    asyncio.run(tool.execute(
        ref="topic:rabies", name="Rabies",
        derived_from=["reference:rabies-investigation", "not-a-ref"],
        body="Notes distilled from the investigation."))
    page = _page(tmp_path, "topic:rabies")
    # the valid reference ref is linked; the non-reference value is skipped
    assert page.derived_from == ["reference:rabies-investigation"]
    prov = page.provenance["derived_from"]["reference:rabies-investigation"]
    assert prov["author"] == "agent"


def test_tool_rejects_bad_ref(tmp_path):
    tool = MemoryUpsertEntityTool(workspace=str(tmp_path))
    out = asyncio.run(tool.execute(ref="not-a-ref", name="X"))
    assert "error" in out


def test_tool_dangling_relation_allowed(tmp_path):
    # relation to a non-existent target is allowed (gap #1) — no placeholder.
    tool = MemoryUpsertEntityTool(workspace=str(tmp_path))
    asyncio.run(tool.execute(
        ref="company:x", name="X",
        relations=[{"to": "person:ghost", "type": "founded_by"}]))
    page = _page(tmp_path, "company:x")
    assert any(r["to"] == "person:ghost" for r in page.relations)
    # the target page need not exist
    assert not (tmp_path / "memory/entities/person/ghost.md").exists()


def test_tool_schema_and_name():
    tool = MemoryUpsertEntityTool(workspace="/tmp/_x")
    assert tool.name == "memory_upsert_entity"
    schema = tool.to_schema()
    fn = schema["function"] if "function" in schema else schema
    # description present + params include ref/name/relations/body
    text = str(fn)
    assert "ref" in text and "relations" in text and "body" in text
    assert "memory_ingest for documents" in tool.description
