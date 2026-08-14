import asyncio

from durin.agent.tools.context import RequestContext
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


def _fake_ingest_result(tmp_path, source, captured):
    def _fake(workspace, src, **kwargs):
        captured.update(kwargs)
        return {
            "id": "x", "source": str(src), "meta_path": str(tmp_path / "meta.json"),
            "size_bytes": 1, "content": "some text", "job_id": None,
        }
    return _fake


def test_execute_threads_the_session_key_from_context_into_ingest(tmp_path, monkeypatch):
    """A session-scoped ingest's background OCR job must be tagged with that
    session -- otherwise the job's session_key stays NULL in the registry and
    it never appears in anyone's tasks tray (JobRegistry.list_for_session
    filters with `WHERE session_key = ?`, which SQL never matches NULL)."""
    doc = tmp_path / "rabies.md"
    doc.write_text("# Rabies\n\nA viral disease.\n", encoding="utf-8")
    captured: dict = {}
    monkeypatch.setattr(
        "durin.agent.tools.memory_ingest.ingest_artifact",
        _fake_ingest_result(tmp_path, doc, captured),
    )

    tool = MemoryIngestTool(workspace=str(tmp_path))
    tool.set_context(RequestContext(channel="websocket", chat_id="c1"))
    asyncio.run(tool.execute(path=str(doc)))

    assert captured.get("session_key") == "websocket:c1"


def test_execute_prefers_the_explicit_session_key_over_channel_and_chat_id(tmp_path, monkeypatch):
    doc = tmp_path / "rabies.md"
    doc.write_text("# Rabies\n\nA viral disease.\n", encoding="utf-8")
    captured: dict = {}
    monkeypatch.setattr(
        "durin.agent.tools.memory_ingest.ingest_artifact",
        _fake_ingest_result(tmp_path, doc, captured),
    )

    tool = MemoryIngestTool(workspace=str(tmp_path))
    tool.set_context(RequestContext(channel="websocket", chat_id="c1", session_key="unified:main"))
    asyncio.run(tool.execute(path=str(doc)))

    assert captured.get("session_key") == "unified:main"


def test_execute_passes_no_session_key_without_a_request_context(tmp_path, monkeypatch):
    # No set_context call at all -- the tool must still work (session_key=None
    # is exactly what ingest_artifact already defaults to), not raise.
    doc = tmp_path / "rabies.md"
    doc.write_text("# Rabies\n\nA viral disease.\n", encoding="utf-8")
    captured: dict = {}
    monkeypatch.setattr(
        "durin.agent.tools.memory_ingest.ingest_artifact",
        _fake_ingest_result(tmp_path, doc, captured),
    )

    tool = MemoryIngestTool(workspace=str(tmp_path))
    asyncio.run(tool.execute(path=str(doc)))

    assert captured.get("session_key") is None
