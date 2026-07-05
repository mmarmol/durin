"""SP1: MemoryService — focused unit tests.

Calls the service directly (no HTTP) using a tmp memory workspace seeded the
same way ``test_memory_entry_endpoints.py`` and ``test_graph_api_entries.py``
do.  Covers: a read (entry), the forget mutation (all 4 result/status cases),
an async (search, mocked backend), and a path-seg route (entity 404).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from durin.service.memory import (
    ForgetResult,
    MemoryDocumentQuery,
    MemoryDocumentsQuery,
    MemoryEntityQuery,
    MemoryEntryQuery,
    MemoryForgetCommand,
    MemoryResult,
    MemorySearchQuery,
    MemoryService,
)
from durin.service.principal import Principal
from durin.service.types import ForbiddenError, NotFoundError, ValidationFailedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_entry(
    ws: Path,
    *,
    class_name: str,
    entry_id: str,
    body: str = "obs",
    entities: tuple[str, ...] = ("person:alice",),
) -> Path:
    ent_lines = (
        "entities:\n" + "".join(f"  - {e}\n" for e in entities)
        if entities
        else ""
    )
    fm = (
        f"id: {entry_id}\n"
        f"headline: {entry_id} headline\n"
        f"valid_from: 2026-05-30\n"
        f"{ent_lines}"
    )
    p = ws / "memory" / class_name / f"{entry_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return p


def _service(tmp_path: Path) -> MemoryService:
    return MemoryService(workspace_resolver=lambda: tmp_path)


# ---------------------------------------------------------------------------
# Read: entry (happy path + 404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_returns_payload(tmp_path: Path) -> None:
    _seed_entry(tmp_path, class_name="episodic", entry_id="obs-1", body="Alice loves rust")
    svc = _service(tmp_path)
    result = await svc.entry(MemoryEntryQuery(uri="memory/episodic/obs-1"), Principal.local())
    assert isinstance(result, MemoryResult)
    assert result.data["uri"] == "memory/episodic/obs-1"
    assert result.data["class_name"] == "episodic"
    assert "Alice loves rust" in result.data["body"]


@pytest.mark.asyncio
async def test_entry_raises_not_found(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    with pytest.raises(NotFoundError) as exc_info:
        await svc.entry(MemoryEntryQuery(uri="memory/episodic/ghost"), Principal.local())
    assert "ghost" in exc_info.value.message


# ---------------------------------------------------------------------------
# Write: forget — all 4 result/status cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_archives_entry(tmp_path: Path) -> None:
    _seed_entry(tmp_path, class_name="episodic", entry_id="obs-2")
    svc = _service(tmp_path)
    result = await svc.forget(MemoryForgetCommand(uri="memory/episodic/obs-2"), Principal.local())
    assert isinstance(result, ForgetResult)
    assert result.result == "archived"
    assert not (tmp_path / "memory" / "episodic" / "obs-2.md").exists()
    assert (tmp_path / "memory" / "archive" / "episodic" / "obs-2.md").exists()


@pytest.mark.asyncio
async def test_forget_not_found(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    with pytest.raises(NotFoundError) as exc:
        await svc.forget(
            MemoryForgetCommand(uri="memory/episodic/nonexistent"), Principal.local()
        )
    assert exc.value.details["result"] == "not_found"


@pytest.mark.asyncio
async def test_forget_protected(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    with pytest.raises(ForbiddenError) as exc:
        await svc.forget(
            MemoryForgetCommand(uri="memory/entities/person/marcelo"), Principal.local()
        )
    assert exc.value.details["result"] == "protected"


@pytest.mark.asyncio
async def test_forget_invalid_uri(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    with pytest.raises(ValidationFailedError) as exc:
        await svc.forget(MemoryForgetCommand(uri="garbage"), Principal.local())
    assert exc.value.details["result"] == "invalid"


# ---------------------------------------------------------------------------
# Async read: search (mocked backend)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_calls_api(tmp_path: Path) -> None:
    fake_payload = {"results": [{"uri": "memory/episodic/x", "score": 0.9}]}
    fake_cfg = SimpleNamespace(
        workspace_path=tmp_path,
        memory=SimpleNamespace(enabled=False, embedding=SimpleNamespace(model="")),
    )
    with (
        patch("durin.config.loader.load_config", return_value=fake_cfg),
        patch(
            "durin.memory.graph_api.search_memory_api",
            new=AsyncMock(return_value=fake_payload),
        ),
    ):
        svc = _service(tmp_path)
        result = await svc.search(
            MemorySearchQuery(q="Alice", scope="all", level="warm", kinds="all"),
            Principal.local(),
        )
    assert isinstance(result, MemoryResult)
    assert result.data == fake_payload


# ---------------------------------------------------------------------------
# Path-seg route: entity 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_raises_not_found(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    with pytest.raises(NotFoundError):
        await svc.entity(MemoryEntityQuery(ref="person:nobody"), Principal.local())


# ---------------------------------------------------------------------------
# Read: reference documents (the Library shelf) — list + detail + 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_documents_lists_ingested_references(tmp_path: Path) -> None:
    from durin.memory.reference import ingest_reference

    ingest_reference(tmp_path, "A Book", "# H\n\nbody.\n")
    svc = _service(tmp_path)
    result = await svc.documents(MemoryDocumentsQuery(), Principal.local())
    assert isinstance(result, MemoryResult)
    docs = result.data["documents"]
    assert len(docs) == 1
    assert docs[0]["title"] == "A Book"
    assert docs[0]["ref"] == "reference:a-book"


@pytest.mark.asyncio
async def test_document_detail_returns_payload(tmp_path: Path) -> None:
    from durin.memory.reference import ingest_reference

    ingest_reference(tmp_path, "A Book", "# Intro\n\nHello.\n", source="disk:/a.pdf")
    svc = _service(tmp_path)
    result = await svc.document(MemoryDocumentQuery(slug="a-book"), Principal.local())
    assert isinstance(result, MemoryResult)
    assert result.data["title"] == "A Book"
    assert result.data["source"] == "disk:/a.pdf"
    assert result.data["outline"] is None  # not distilled
    assert result.data["chunks_preview"]


@pytest.mark.asyncio
async def test_document_detail_raises_not_found(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    with pytest.raises(NotFoundError) as exc_info:
        await svc.document(MemoryDocumentQuery(slug="ghost"), Principal.local())
    assert "ghost" in exc_info.value.message


@pytest.mark.asyncio
async def test_documents_requires_memory_read(tmp_path: Path) -> None:
    restricted = Principal.remote(subject="t1", scopes=frozenset())
    svc = _service(tmp_path)
    with pytest.raises(ForbiddenError):
        await svc.documents(MemoryDocumentsQuery(), restricted)


# ---------------------------------------------------------------------------
# Scope enforcement: ForbiddenError when principal lacks the scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_requires_memory_read(tmp_path: Path) -> None:
    restricted = Principal.remote(subject="t1", scopes=frozenset())
    svc = _service(tmp_path)
    with pytest.raises(ForbiddenError):
        await svc.entry(MemoryEntryQuery(uri="memory/episodic/x"), restricted)


@pytest.mark.asyncio
async def test_forget_requires_memory_write(tmp_path: Path) -> None:
    restricted = Principal.remote(subject="t1", scopes=frozenset())
    svc = _service(tmp_path)
    with pytest.raises(ForbiddenError):
        await svc.forget(MemoryForgetCommand(uri="memory/episodic/x"), restricted)
