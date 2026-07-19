from __future__ import annotations

import re as _re
from datetime import datetime
from datetime import timezone as _tz
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.entity_page import EntityPage
from durin.memory.extract_runner import load_session

_READ_PARAMS = tool_parameters_schema(
    ref=StringSchema("Entity ref '<type>:<slug>' (e.g. 'place:torrent')."),
    required=["ref"],
    description=(
        "Read one entity's COMPLETE page (frontmatter + attributes + relations "
        "+ provenance + body). Reach for this after memory_search points you at "
        "an entity and you need the whole structured page, not just the search "
        "preview. (For a quick body-only follow-up on a preview hit, "
        "memory_drill is enough.)"
    ),
)


def _page_path(workspace: Path, ref: str) -> Path:
    type_, _, slug = ref.partition(":")
    return Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"


@tool_parameters(_READ_PARAMS)
class MemoryReadEntityTool(Tool):
    _scopes = {"core", "subagent"}

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_read_entity"

    @property
    def description(self) -> str:
        return _READ_PARAMS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        ref = (kwargs.get("ref") or "").strip()
        if ":" not in ref:
            return {"error": "ref must be '<type>:<slug>'"}
        path = _page_path(self._workspace, ref)
        if not path.exists():
            return {"error": f"no entity {ref}"}
        page = EntityPage.from_file(path)
        if page is None:
            return {"error": f"unreadable {ref}"}
        return {"ref": ref, "markdown": page.to_markdown()}


_LINEAGE_PARAMS = tool_parameters_schema(
    ref=StringSchema("Entity ref '<type>:<slug>'."),
    required=["ref"],
    description=(
        "The git history of an entity: who changed it, when, and why (including "
        "absorb/merge commits). Use to gauge an entity before you rely on or "
        "edit it — is it long-established or freshly created, has it been merged "
        "from others."
    ),
)


@tool_parameters(_LINEAGE_PARAMS)
class MemoryEntityLineageTool(Tool):
    _scopes = {"core", "subagent"}

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_entity_lineage"

    @property
    def description(self) -> str:
        return _LINEAGE_PARAMS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        ref = (kwargs.get("ref") or "").strip()
        type_, _, slug = ref.partition(":")
        rel = f"entities/{type_}/{slug}.md".encode()
        root = self._workspace / "memory"
        try:
            from dulwich.repo import Repo
            repo = Repo(str(root))
            out = []
            for entry in repo.get_walker(paths=[rel], max_entries=20):
                c = entry.commit
                out.append({
                    "sha": c.id.decode()[:10],
                    "when": datetime.fromtimestamp(c.author_time, _tz.utc).isoformat(),
                    "author": c.author.decode("utf-8", "replace"),
                    "message": c.message.decode("utf-8", "replace").strip(),
                })
            return {"ref": ref, "commits": out}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"lineage unavailable: {exc}", "commits": []}


_SRC_PARAMS = tool_parameters_schema(
    ref=StringSchema("Entity ref '<type>:<slug>'."),
    required=["ref"],
    description=(
        "Read the original conversation turns an entity was distilled from (its "
        "provenance source_refs + derived_from). Use when a fact looks off, or "
        "when you need the exact wording and context that produced it, not the "
        "summary."
    ),
)
_SRC_RE = _re.compile(r"\[\[sessions/(.+?)\.md#turn-(\d+)\]\]")


def _source_refs(page) -> list[str]:
    # v1 scope: derived_from + per-attribute provenance source_refs — where the
    # bulk of dream-distilled facts come from. Relation-level provenance is
    # intentionally out of scope here.
    refs: list[str] = list(getattr(page, "derived_from", []) or [])
    prov = getattr(page, "provenance", {}) or {}
    for field in (prov.get("attributes") or {}).values():
        sr = field.get("source_ref") if isinstance(field, dict) else None
        if sr:
            refs.append(sr)
    seen, out = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


@tool_parameters(_SRC_PARAMS)
class MemorySourceSessionTool(Tool):
    _scopes = {"core", "subagent"}

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_source_session"

    @property
    def description(self) -> str:
        return _SRC_PARAMS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        ref = (kwargs.get("ref") or "").strip()
        type_, _, slug = ref.partition(":")
        path = Path(self._workspace) / "memory" / "entities" / type_ / f"{slug}.md"
        if not path.exists():
            return {"error": f"no entity {ref}", "sources": []}
        page = EntityPage.from_file(path)
        if page is None:
            return {"error": f"unreadable {ref}", "sources": []}
        out = []
        for sr in _source_refs(page):
            m = _SRC_RE.search(sr)
            if not m:
                continue
            key, n = m.group(1), int(m.group(2))
            jl = Path(self._workspace) / "sessions" / f"{key}.jsonl"
            if not jl.exists():
                continue
            try:
                _meta, msgs = load_session(jl)
            except Exception:  # noqa: BLE001
                continue
            if 1 <= n <= len(msgs):
                content = msgs[n - 1].get("content")
                out.append({"ref": sr, "turn": n,
                            "content": content if isinstance(content, str) else str(content)})
        return {"ref": ref, "sources": out}
