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


_DOC_PARAMS = tool_parameters_schema(
    ref=StringSchema("Entity ref '<type>:<slug>'."),
    required=["ref"],
    description=(
        "Read the reference documents an entity was extracted from (its "
        "derived_from documents and [[references/...]] source refs): for each, "
        "an excerpt around the places the document names the entity (its name "
        "or an alias), or the document's opening when it names it nowhere. Use "
        "when a page is thin — a sentence or two, no relations — to see what "
        "the document itself says about the entity."
    ),
)
_DOC_CITE_RE = _re.compile(r"\[\[references/(.+?)\.md\]\]")
# What one call returns is bounded: a few thousand characters per document and
# a total below the investigating judge's per-result ceiling, past which the
# runner cuts the result at its head and every later document would be lost.
_DOC_EXCERPT_CHARS = 3000
_DOC_CALL_CHARS = 6000
_DOC_MAX_DOCUMENTS = 4
# A mention is shown with a little text before it and more after it: what a
# document says about a thing mostly follows the heading or sentence naming it.
_DOC_BEFORE_CHARS = 300
_DOC_AFTER_CHARS = 900
_DOC_GAP = "\n[…]\n"


def _cited_documents(page: EntityPage, text: str) -> list[str]:
    """Slugs of the reference documents a page cites, in first-seen order: its
    ``derived_from`` refs, then every ``[[references/<slug>.md]]`` source ref in
    the page file (field provenance and body-section markers)."""
    slugs = [d[len("reference:"):] for d in page.derived_from or []
             if d.startswith("reference:")]
    slugs += _DOC_CITE_RE.findall(text)
    return list(dict.fromkeys(s for s in slugs if s))


def _mention_terms(page: EntityPage, slug: str) -> list[str]:
    """The names a document may use for the entity: its name, its aliases and
    its slug read as words, each once regardless of case."""
    out: dict[str, str] = {}
    for term in (page.name, *page.aliases, slug.replace("-", " ")):
        term = " ".join(str(term).split())
        if len(term) >= 2:
            out.setdefault(term.lower(), term)
    return list(out.values())


def _term_pattern(term: str) -> _re.Pattern[str]:
    """Case-insensitive match of ``term`` as whole words; any whitespace run
    between its words matches, so a line break inside the name still counts."""
    body = r"\s+".join(_re.escape(w) for w in term.split())
    left = r"\b" if term[0].isalnum() else ""
    right = r"\b" if term[-1].isalnum() else ""
    return _re.compile(left + body + right, _re.IGNORECASE)


def _excerpt(body: str, terms: list[str], budget: int) -> tuple[list[str], str]:
    """The part of a document shown for an entity: windows around the places
    it names the entity, in document order, within ``budget`` characters — or
    the document's opening when it names the entity nowhere. Returns the
    terms found and the excerpt."""
    matched: list[str] = []
    hits: set[int] = set()
    for term in terms:
        found = {m.start() for m in _term_pattern(term).finditer(body)}
        if found:
            matched.append(term)
            hits |= found
    if not hits:
        return [], body[:budget]
    windows: list[list[int]] = []
    for pos in sorted(hits):
        start = max(0, pos - _DOC_BEFORE_CHARS)
        end = min(len(body), pos + _DOC_AFTER_CHARS)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    parts: list[str] = []
    room = budget
    for start, end in windows:
        if parts:
            room -= len(_DOC_GAP)
        if room <= 0:
            break
        end = min(end, start + room)
        parts.append(body[start:end])
        room -= end - start
    return matched, _DOC_GAP.join(parts)


@tool_parameters(_DOC_PARAMS)
class MemorySourceDocumentTool(Tool):
    """Evidence for the investigating merge judge: a page auto-extracted from a
    reference document is often a sentence long, and the document is where
    the facts that tell two such pages apart live.

    Registered explicitly by the Tier-2 judge (``tier2_judge._build_tools``);
    the ``dream`` scope keeps it off the auto-discovered agent surfaces, where
    ``memory_drill`` already reads a whole document by its ``reference:`` uri.
    Reads only files that resolve inside ``memory/references/``."""

    _scopes = {"dream"}

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_source_document"

    @property
    def description(self) -> str:
        return _DOC_PARAMS["description"]

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
        if not path.is_file():
            return {"error": f"no entity {ref}"}
        text = path.read_text(encoding="utf-8")
        page = EntityPage.from_text(text)
        if page is None:
            return {"error": f"unreadable {ref}"}
        slugs = _cited_documents(page, text)
        terms = _mention_terms(page, ref.partition(":")[2])
        shown = slugs[:_DOC_MAX_DOCUMENTS]
        documents: list[dict[str, Any]] = []
        left = _DOC_CALL_CHARS
        for i, slug in enumerate(shown):
            budget = min(_DOC_EXCERPT_CHARS, left // (len(shown) - i))
            record = self._read_document(slug, terms, budget)
            left -= len(record.get("excerpt", ""))
            documents.append(record)
        out: dict[str, Any] = {"ref": ref, "documents": documents}
        if len(slugs) > len(shown):
            out["documents_not_shown"] = len(slugs) - len(shown)
        return out

    def _read_document(self, slug: str, terms: list[str], budget: int) -> dict[str, Any]:
        from durin.memory.storage import FrontmatterError, split_frontmatter

        doc = f"reference:{slug}"
        library = (self._workspace / "memory" / "references").resolve()
        try:
            path = (library / f"{slug}.md").resolve()
        except (OSError, ValueError):
            return {"doc": doc, "error": "not a readable document ref"}
        if not path.is_relative_to(library):
            return {"doc": doc, "error": "outside the reference library; not read"}
        if not path.is_file():
            return {"doc": doc, "error": "document not found"}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"doc": doc, "error": f"unreadable: {exc}"}
        try:
            meta, body = split_frontmatter(text)
        except FrontmatterError:
            meta, body = {}, text
        matched, excerpt = _excerpt(body.strip(), terms, budget)
        return {"doc": doc, "title": str(meta.get("title") or slug), "chars": len(body),
                "matched": matched, "excerpt": excerpt}
