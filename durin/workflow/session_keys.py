"""Where a workflow node's conversation lives.

The key is needed in two places: the node runner, which saves the session under
it, and the engine, which advertises it on the run manifest so a reader
arriving mid-node can open what is running. Deriving it twice invites drift —
a persistent-session node omits the iteration suffix and a fan-out worker adds
a worker suffix, so a second implementation that missed either would point a
reader at a session that does not exist. One derivation, both callers.
"""

from __future__ import annotations

from typing import Any


def is_persistent_session(
    node: Any, *, worker_index: int | None = None, isolated: bool = False
) -> bool:
    """Does this node keep ONE session across its visits?

    Parallel units never do: a fan-out worker (``worker_index``) and a branch
    running against a private workspace fork (``isolated``) each get their own
    session per unit. The parser also rejects persistence on those, so this is
    defense in depth rather than the only guard.
    """
    return (
        getattr(node, "session", "fresh") == "persistent"
        and worker_index is None
        and not isolated
    )


def node_session_key(
    run_id: str,
    node: Any,
    iteration: int,
    *,
    worker_index: int | None = None,
    isolated: bool = False,
) -> str:
    """``workflow:<run>:<node>`` for a persistent session, else with the
    iteration appended, plus the worker index for a fan-out unit."""
    if is_persistent_session(node, worker_index=worker_index, isolated=isolated):
        return f"workflow:{run_id}:{node.id}"
    if worker_index is not None:
        return f"workflow:{run_id}:{node.id}:{iteration}:{worker_index}"
    return f"workflow:{run_id}:{node.id}:{iteration}"
