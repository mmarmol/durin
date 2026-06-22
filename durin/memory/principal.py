"""Principal resolution + the pinned hot-context.

The "user" of a message is resolved PER-MESSAGE: channel-id → owner (config) →
``person:anonymous``. The pinned context (always injected, independent of
retrieval) is the principal's person entity + the ``always_on`` feedback
entities (stance/practice the dream marked always_on). This closes the loop:
authored knowledge is re-injected so the agent actually uses it.

USER.md / MEMORY.md dissolve into this dynamic composition.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity

__all__ = [
    "ANONYMOUS",
    "resolve_principal",
    "ensure_owner",
    "mark_always_on",
    "list_always_on",
    "build_pinned_context",
]

ANONYMOUS = "person:anonymous"


def resolve_principal(channel_id: str | None, *, owner: str | None = None,
                      channel_map: dict[str, str] | None = None) -> str:
    """Who is the user for this message? channel → owner → anonymous."""
    if channel_id and channel_map and channel_id in channel_map:
        return channel_map[channel_id]
    if owner:
        return owner
    return ANONYMOUS


def _page_path(workspace: Path, ref: str) -> Path:
    type_, _, slug = ref.partition(":")
    return Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"


def ensure_owner(workspace: Path, owner_ref: str, *, name: str | None = None) -> bool:
    """Cold-start: create a placeholder person entity for the owner if missing.

    Returns True if it created one. The placeholder is dream-authored so the
    agent can enrich it later without precedence conflicts.
    """
    if _page_path(workspace, owner_ref).exists():
        return False
    _type, _, slug = owner_ref.partition(":")
    write_entity(
        workspace, owner_ref,
        [FieldPatch(kind="body_append", value="(auto-created owner placeholder)",
                    author="dream", source_ref="cold_start",
                    at=datetime.now(timezone.utc))],
        create=True, name=name or slug,
    )
    return True


def mark_always_on(workspace: Path, ref: str, on: bool = True) -> None:
    """Mark a feedback entity always_on (dream-owned attribute)."""
    write_entity(
        workspace, ref,
        [FieldPatch(kind="attribute", key="always_on", value=bool(on),
                    author="dream", source_ref="hot_layer_policy",
                    at=datetime.now(timezone.utc))],
        create=True,
    )


def list_always_on(workspace: Path) -> list[str]:
    """Entity refs whose always_on attribute is truthy."""
    root = Path(workspace) / "memory" / "entities"
    out: list[str] = []
    if not root.exists():
        return out
    for md in sorted(root.rglob("*.md")):
        page = EntityPage.from_file(md)
        if page and page.attributes.get("always_on"):
            out.append(f"{md.parent.name}:{md.stem}")
    return out


def _load(workspace: Path, ref: str) -> EntityPage | None:
    p = _page_path(workspace, ref)
    return EntityPage.from_file(p) if p.exists() else None


def _render_pinned_block(page: EntityPage) -> str:
    lines = [f"### {page.name} ({page.type})"]
    if page.attributes:
        attrs = ", ".join(
            f"{k}: {v}" for k, v in page.attributes.items() if k != "always_on"
        )
        if attrs:
            lines.append(attrs)
    if page.body:
        body = "\n".join(
            ln for ln in page.body.splitlines() if not ln.strip().startswith("<!--")
        )
        if body.strip():
            lines.append(body.strip())
    return "\n".join(lines).strip()


def build_pinned_context(workspace: Path, principal_ref: str) -> str:
    """The always-injected layer: who the user is + always_on feedback."""
    parts: list[str] = []
    principal = _load(workspace, principal_ref)
    if principal:
        parts.append("## Who you're talking to\n\n" + _render_pinned_block(principal))
    pins: list[str] = []
    for ref in list_always_on(workspace):
        if ref == principal_ref:
            continue
        page = _load(workspace, ref)
        if page:
            pins.append(_render_pinned_block(page))
    if pins:
        parts.append("## Always-on guidance\n\n" + "\n\n".join(pins))
    return "\n\n".join(parts)
