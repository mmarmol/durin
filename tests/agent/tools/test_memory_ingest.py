import asyncio

from durin.agent.tools.memory_ingest import MemoryIngestTool


def test_ingest_result_emits_id_and_reference_before_content(tmp_path):
    # C1: `id` + `reference` must precede `content` so they survive the 16 KB
    # head-truncation of tool results on large documents.
    doc = tmp_path / "rabies.md"
    doc.write_text("# Rabies\n\nA viral disease.\n", encoding="utf-8")

    tool = MemoryIngestTool(workspace=str(tmp_path))
    out = asyncio.run(tool.execute(path=str(doc)))

    assert "error" not in out
    assert "reference" in out and out["reference"].startswith("reference:")
    keys = list(out.keys())
    assert keys.index("id") < keys.index("content")
    assert keys.index("reference") < keys.index("content")
