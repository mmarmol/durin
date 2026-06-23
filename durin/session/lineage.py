"""Lineage metadata contract for sessions spawned by another session.

Stored inside the open ``Session.metadata`` dict — no schema migration. A
"branch" session (a subagent today; a workflow stage later) records who
spawned it and the root of its tree, so its work is navigable and never
orphaned. These keys are *identity* metadata: they live on line 0 of the
session ``.jsonl`` (they are deliberately NOT in ``SessionManager``'s
derived/volatile sidecar key sets), so ``list_sessions`` / ``children_of``
can read them from the header without loading the whole file.
"""

from __future__ import annotations

from typing import Any

PARENT_SESSION_ID = "parent_session_id"
ROOT_ID = "root_id"
ORIGIN_TYPE = "origin_type"   # "subagent" | "workflow_node" | ...
ORIGIN_ID = "origin_id"       # the spawning task / node id


def build_lineage(
    *, parent_session_id: str, root_id: str, origin_type: str, origin_id: str
) -> dict[str, str]:
    """Build the lineage metadata block for a branch session."""
    return {
        PARENT_SESSION_ID: parent_session_id,
        ROOT_ID: root_id,
        ORIGIN_TYPE: origin_type,
        ORIGIN_ID: origin_id,
    }


def parent_of(metadata: dict[str, Any]) -> str | None:
    """The parent session key, or None if this is not a branch session."""
    val = metadata.get(PARENT_SESSION_ID)
    return val if isinstance(val, str) else None


def root_of(metadata: dict[str, Any], *, default: str) -> str:
    """The tree root key. Falls back to *default* when unset (a top-level parent)."""
    val = metadata.get(ROOT_ID)
    return val if isinstance(val, str) else default
