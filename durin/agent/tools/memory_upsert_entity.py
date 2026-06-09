"""memory_upsert_entity tool — the agent authors/updates an entity directly.

Design §2.2/§2.4 + decision b: the agent provides name + aliases + relations +
body (prose); the system (dream) owns structured attributes. The entity exists
immediately via ``memory_writer`` (optimistic CAS). Merge semantics; dangling
relations allowed; dedup deferred to dream.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import (
    ArraySchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.provenance import author_scope

_DESCRIPTION = (
    "Author or update an entity (a person, company, product, topic, place, "
    "etc.) you have learned a fact about. Provide `ref` as `<type>:<slug>` "
    "(e.g. company:mxhero, person:marcelo), the display `name`, any `aliases`, "
    "`relations` to other entities ({to: '<type>:<slug>', type: 'partner'}), "
    "and prose `body` describing what you know. Merges into the existing entity "
    "if it exists, creates it otherwise. Do NOT pass structured attributes — "
    "the system extracts those from your prose. When this entity was distilled "
    "from a document you ingested, pass `derived_from` with the "
    "`reference:<slug>` ref(s) memory_ingest returned, so the entity links back "
    "to its sources. Use this for facts about a THING; use memory_ingest for "
    "documents. By default the `body` is APPENDED to what is already there "
    "(nothing is lost). Pass `body_mode: \"replace\"` only when you are "
    "rewriting the whole body to correct or clean it up — and only when you "
    "have the full current body in context. A replace cannot overwrite prose a "
    "user authored (it degrades to an append); git history preserves prior "
    "versions either way."
)

_PARAMETERS = tool_parameters_schema(
    description=_DESCRIPTION,
    required=["ref"],
    ref=StringSchema(
        "Entity reference '<type>:<slug>', lowercase slug — e.g. "
        "company:mxhero, person:marcelo, topic:smtp."
    ),
    name=StringSchema("Display name (required when creating a new entity)."),
    aliases=ArraySchema(
        StringSchema("alternate name"),
        description="Alternate names / identifiers for this entity.",
        nullable=True,
    ),
    relations=ArraySchema(
        ObjectSchema(
            to=StringSchema("target entity ref <type>:<slug>"),
            type=StringSchema("relation kind, e.g. partner, makes, works_at"),
            required=["to", "type"],
            additional_properties=True,
        ),
        description="Relations to other entities.",
        nullable=True,
    ),
    derived_from=ArraySchema(
        StringSchema("source document ref 'reference:<slug>'"),
        description=(
            "Source documents this entity was distilled from — the "
            "`reference:<slug>` ref(s) returned by memory_ingest."
        ),
        nullable=True,
    ),
    body=StringSchema(
        "Prose describing what you know about this entity.", nullable=True
    ),
    body_mode=StringSchema(
        "How to apply `body`: 'append' (default) adds an attributed section "
        "without losing prior prose; 'replace' overwrites the whole body with "
        "the text you pass. Use 'replace' only to correct/rewrite the full "
        "body. A replace over a user-authored body degrades to an append.",
        enum=["append", "replace"],
        nullable=True,
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryUpsertEntityTool(Tool):
    """memory_upsert_entity — agent-authored entity pages (design §2.4)."""

    config_key = "memory"

    def __init__(self, workspace: str | Path, app_config: Any | None = None) -> None:
        self._workspace = Path(workspace).expanduser()
        self._app_config = app_config

    @property
    def name(self) -> str:
        return "memory_upsert_entity"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            workspace=ctx.workspace,
            app_config=getattr(ctx, "app_config", None),
        )

    async def execute(self, **kwargs: Any) -> Any:
        ref = (kwargs.get("ref") or "").strip()
        if not ref or ":" not in ref:
            return {"error": "ref must be '<type>:<slug>' (e.g. company:mxhero)"}
        now = datetime.now(timezone.utc)
        src = _source_ref()
        patches: list[FieldPatch] = []
        for a in (kwargs.get("aliases") or []):
            patches.append(FieldPatch(
                kind="alias", value=str(a), author=None, source_ref=src, at=now))
        for r in (kwargs.get("relations") or []):
            if isinstance(r, dict) and r.get("to") and r.get("type"):
                patches.append(FieldPatch(
                    kind="relation", value=dict(r), author=None,
                    source_ref=src, at=now))
        for d in (kwargs.get("derived_from") or []):
            d = str(d).strip()
            if d.startswith("reference:"):
                patches.append(FieldPatch(
                    kind="derived_from", value=d, author=None,
                    source_ref=src, at=now))
        body = kwargs.get("body")
        if body:
            body_mode = str(kwargs.get("body_mode") or "append")
            body_kind = "body_replace" if body_mode == "replace" else "body_append"
            patches.append(FieldPatch(
                kind=body_kind, value=str(body), author=None,
                source_ref=src, at=now))
        # §2.13: explicitly (re-)authoring an entity overrides a prior delete
        # tombstone — the user asked for it back.
        from durin.memory.deletion import clear_delete_tombstone
        clear_delete_tombstone(self._workspace, ref)
        try:
            with author_scope("agent_created"):
                result = write_entity(
                    self._workspace, ref, patches,
                    create=True, name=kwargs.get("name"),
                )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"upsert failed: {exc}"}
        emit_tool_event(
            "memory.upsert_entity",
            {"ref": ref, "committed": result.committed, "retries": result.retries},
        )
        return {"ref": ref, "committed": result.committed}


def _source_ref() -> str:
    """Per-turn provenance source: the session turn if available, else a tag."""
    try:
        from durin.agent.tools.memory_store import _session_turn_ref

        return _session_turn_ref() or "memory_upsert_entity"
    except Exception:  # pragma: no cover — best effort
        return "memory_upsert_entity"
