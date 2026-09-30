"""A workflow node's tools resolve a relative path in the node's working folder.

A node's prompt says "Your working directory for this run is: <folder>", but its
file tools resolved relative paths against the workspace root: in one run eleven
relative reads failed across four nodes, and one failure suggested another
ticket's stray file at the workspace root. The node runner hands its tools the
working folder (``ToolContext.work_dir``) as their base for relative paths.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.tools.context import AuxProviderHandle, ToolContext
from durin.agent.tools.convert_to_markdown import ConvertToMarkdownTool
from durin.agent.tools.file_state import FileStates
from durin.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from durin.agent.tools.interpret_audio import InterpretAudioTool
from durin.agent.tools.interpret_image import InterpretImageTool
from durin.agent.tools.search import GrepTool
from durin.config.schema import ToolsConfig
from durin.providers.base import LLMResponse

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path, ToolContext]:
    ws = (tmp_path / "ws").resolve()
    work = ws / ".workflow" / "r1" / "work"
    work.mkdir(parents=True)
    (work / "ticket.json").write_text('{"id": 23164}', encoding="utf-8")
    # Another run's file, left at the workspace root.
    (ws / "slack-context.json").write_text("another ticket", encoding="utf-8")
    aux_provider = MagicMock()
    aux_provider.chat = AsyncMock(return_value=LLMResponse(content="seen", finish_reason="stop"))
    aux = AuxProviderHandle(provider=aux_provider, model="aux-model")
    ctx = ToolContext(config=ToolsConfig(), workspace=str(ws), work_dir=str(work),
                      file_state_store=FileStates(), aux_providers={"vision": aux, "audio": aux})
    return ws, work, ctx


@pytest.mark.asyncio
async def test_a_relative_read_finds_the_file_in_the_working_folder(layout) -> None:
    ws, work, ctx = layout
    read = ReadFileTool.create(ctx)
    assert "23164" in await read.execute(path="ticket.json")
    missing = await read.execute(path="context.json")
    assert "File not found" in missing
    assert "slack-context.json" not in missing      # no stray workspace-root file suggested


@pytest.mark.asyncio
async def test_relative_writes_and_edits_land_in_the_working_folder(layout) -> None:
    ws, work, ctx = layout
    await WriteFileTool.create(ctx).execute(path="draft.json", content='{"a": 1}')
    assert (work / "draft.json").exists() and not (ws / "draft.json").exists()
    await EditFileTool.create(ctx).execute(path="draft.json", old_text='"a": 1', new_text='"a": 2')
    assert json.loads((work / "draft.json").read_text(encoding="utf-8")) == {"a": 2}


@pytest.mark.asyncio
async def test_list_dir_lists_the_working_folder(layout) -> None:
    _, _, ctx = layout
    listing = await ListDirTool.create(ctx).execute(path=".")
    assert "ticket.json" in listing
    assert "slack-context.json" not in listing


@pytest.mark.asyncio
async def test_managed_and_absolute_paths_still_reach_the_workspace(layout) -> None:
    """A workflow script is named from the workspace root (``workflows/...``), as
    in the chat's work area; an absolute path is used as given."""
    ws, _, ctx = layout
    script = ws / "workflows" / "scripts" / "fetch.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('fetched')\n", encoding="utf-8")
    read = ReadFileTool.create(ctx)
    assert "fetched" in await read.execute(path="workflows/scripts/fetch.py")
    assert "another ticket" in await read.execute(path=str(ws / "slack-context.json"))


@pytest.mark.asyncio
async def test_grep_prints_paths_that_read_back_to_the_file_it_found(layout) -> None:
    """A path grep prints is one read_file resolves to the same file: relative to
    the working folder inside it, from the workspace root for a managed area,
    absolute anywhere else."""
    ws, work, ctx = layout
    (work / "notes.md").write_text("MARKER in the folder\n", encoding="utf-8")
    (ws / "memory").mkdir()
    (ws / "memory" / "m.md").write_text("MARKER in memory\n", encoding="utf-8")
    (ws / "repos").mkdir()
    (ws / "repos" / "a.py").write_text("MARKER = 'in a repo'\n", encoding="utf-8")
    grep, read = GrepTool.create(ctx), ReadFileTool.create(ctx)

    assert (await grep.execute(pattern="MARKER", path=".")).splitlines()[0] == "notes.md"

    everywhere = await grep.execute(pattern="MARKER", path=str(ws))
    printed = [line for line in everywhere.splitlines() if line and not line.startswith("(")]
    assert set(printed) == {"notes.md", "memory/m.md", str(ws / "repos" / "a.py")}
    for path in printed:
        assert "MARKER" in await read.execute(path=path)


@pytest.mark.asyncio
async def test_interpret_image_and_audio_resolve_in_the_working_folder(layout) -> None:
    _, work, ctx = layout
    (work / "shot.png").write_bytes(_PNG)
    assert "seen" in await InterpretImageTool.create(ctx).execute(image_path="shot.png")
    (work / "call.wav").write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32)
    audio = await InterpretAudioTool.create(ctx).execute(audio_path="call.wav")
    assert "not found" not in audio


@pytest.mark.asyncio
async def test_convert_to_markdown_resolves_in_the_working_folder(layout) -> None:
    _, work, ctx = layout
    (work / "attachment.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    result = await ConvertToMarkdownTool.create(ctx).execute(path="attachment.csv")
    assert "error" not in result
    assert result["path"] == str(work / "attachment.csv")


@pytest.fixture
def repo(layout):
    """A repository checked out at the workspace root, as the box keeps its code
    under mxhero-repos/."""
    ws, work, ctx = layout
    code = ws / "mxhero-repos" / "share-links"
    code.mkdir(parents=True)
    (code / "shares.ts").write_text("export const disposition = 'attachment';\n", encoding="utf-8")
    return ws, work, ctx


@pytest.mark.asyncio
async def test_a_folder_at_the_workspace_root_is_read_from_there(repo) -> None:
    """The working folder has no mxhero-repos/, so a relative path into it reads
    the workspace root's."""
    ws, _, ctx = repo
    assert "disposition" in await ReadFileTool.create(ctx).execute(
        path="mxhero-repos/share-links/shares.ts")
    found = await GrepTool.create(ctx).execute(pattern="disposition", path="mxhero-repos/")
    printed = found.splitlines()[0]
    assert printed == str(ws / "mxhero-repos" / "share-links" / "shares.ts")
    # A fresh read state, so the read is not answered "unchanged since last read".
    fresh = dataclasses.replace(ctx, file_state_store=FileStates())
    assert "disposition" in await ReadFileTool.create(fresh).execute(path=printed)
    assert "share-links" in await ListDirTool.create(ctx).execute(path="mxhero-repos")


@pytest.mark.asyncio
async def test_a_file_at_the_workspace_root_never_stands_in_for_a_missing_one(repo) -> None:
    """A stray file another run left at the root is not read in place of this
    run's: only a path into a folder there falls back."""
    _, _, ctx = repo
    missing = await ReadFileTool.create(ctx).execute(path="slack-context.json")
    assert "File not found" in missing and "another ticket" not in missing


@pytest.mark.asyncio
async def test_the_working_folders_copy_wins_and_writes_stay_in_it(repo) -> None:
    ws, work, ctx = repo
    (ws / "mxhero-repos" / "notes.md").write_text("theirs", encoding="utf-8")
    await WriteFileTool.create(ctx).execute(path="mxhero-repos/notes.md", content="ours")
    assert (work / "mxhero-repos" / "notes.md").read_text(encoding="utf-8") == "ours"
    assert (ws / "mxhero-repos" / "notes.md").read_text(encoding="utf-8") == "theirs"
    read = ReadFileTool.create(ctx)
    assert "ours" in await read.execute(path="mxhero-repos/notes.md")
    # A path the working folder does not have is still read from the root's folder.
    assert "disposition" in await read.execute(path="mxhero-repos/share-links/shares.ts")


@pytest.mark.asyncio
async def test_a_parent_path_never_falls_back_to_the_root(repo, tmp_path) -> None:
    _, _, ctx = repo
    (tmp_path / "outside.txt").write_text("outside the workspace", encoding="utf-8")
    result = await ReadFileTool.create(ctx).execute(path="../outside.txt")
    assert "outside the workspace" not in result


@pytest.mark.asyncio
async def test_repo_overview_names_a_folder_by_a_path_that_reads_back(repo) -> None:
    from durin.agent.tools.repo_overview import RepoOverviewTool

    ws, work, ctx = repo
    tool = RepoOverviewTool.create(ctx)
    assert (await tool.execute(path=".")).splitlines()[0] == "# Repository overview: ."
    header = (await tool.execute(path="mxhero-repos")).splitlines()[0]
    named = header.removeprefix("# Repository overview: ")
    assert named == str(ws / "mxhero-repos")
    assert "share-links" in await ListDirTool.create(ctx).execute(path=named)


@pytest.mark.asyncio
async def test_execute_code_runs_its_script_in_the_working_folder(layout) -> None:
    """The script's own open() and the tools it calls read the same file."""
    import sys

    from durin.agent.tools.code_execution import ExecuteCodeTool

    if sys.platform == "win32":
        pytest.skip("execute_code needs Unix sockets")
    _, _, ctx = layout
    code = ("from durin_tools import read_file\n"
            "print('rpc:', read_file(path='ticket.json'))\n"
            "print('open:', open('ticket.json').read())\n")
    out = json.loads(await ExecuteCodeTool.create(ctx).execute(code=code))
    assert "open: {\"id\": 23164}" in out["output"], out


@pytest.mark.asyncio
async def test_memory_ingest_resolves_in_the_working_folder(layout, monkeypatch) -> None:
    from durin.agent.tools.memory_ingest import MemoryIngestTool

    _, work, ctx = layout
    (work / "report.md").write_text("# Report\n", encoding="utf-8")
    seen: dict = {}

    def fake_ingest(workspace, source, **kwargs):
        seen["source"] = source
        return {"id": "x", "source": str(source), "meta_path": "m", "size_bytes": 1,
                "content": "c", "job_id": None}

    monkeypatch.setattr("durin.agent.tools.memory_ingest.ingest_artifact", fake_ingest)
    await MemoryIngestTool.create(ctx).execute(path="report.md")
    assert seen["source"] == work / "report.md"


@pytest.mark.asyncio
async def test_convert_to_markdown_answers_for_the_folder_itself(layout, tmp_path) -> None:
    """"." names a folder, not a document: an error reply, not a crash."""
    _, _, ctx = layout
    chat = ToolContext(config=ToolsConfig(), workspace=str(tmp_path), file_state_store=FileStates())
    for tool_ctx in (ctx, chat):
        tool = ConvertToMarkdownTool.create(tool_ctx)
        for path in (".", "./"):
            assert "error" in await tool.execute(path=path)


@pytest.mark.asyncio
async def test_without_a_working_folder_relative_paths_stay_at_the_workspace_root(tmp_path) -> None:
    (tmp_path / "top.txt").write_text("root file", encoding="utf-8")
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path), file_state_store=FileStates())
    assert "root file" in await ReadFileTool.create(ctx).execute(path="top.txt")
