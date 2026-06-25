"""The typed outcome of a workflow run.

The result is a typed value (never free text that a caller could mistake for
success): a status, the final output, and the per-node trace (which node ran, in
which iteration, what it produced, its persisted session). The trace plus the
persisted node sessions are the run's record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class NodeRun:
    node_id: str
    iteration: int
    output: str
    session_key: str | None = None   # for work nodes that persisted a session
    passed: bool | None = None       # for a binary routing node: did its verdict pass? (None = not a binary routing node)
    route_label: str | None = None   # for a multi-way routing node: the matched case label (None otherwise)
    worker_index: int | None = None  # for a fan-out worker: its index in the batch (None otherwise)
    branch_id: str | None = None     # for a static-parallel branch: the branch node id (None otherwise)
    status: str = "ok"               # "ok" (agent node persisted) | "no_session" (command node) | "persist_failed" (save raised) | "node_failed" (the node's agent turn raised)
    error: str | None = None         # failure detail when status is "node_failed"/"persist_failed" (None otherwise)


@dataclass
class WorkflowResult:
    status: Literal["completed", "needs_input", "exhausted", "aborted"]
    final_output: str | None
    runs: list[NodeRun] = field(default_factory=list)
    run_id: str = ""
    output_dir: str | None = None  # the terminal node's output folder, if any
    exhausted_node: str | None = None  # set when status=="exhausted": the node that hit its budget
    failed_node: str | None = None  # set when a node's agent turn raised: the node that failed
    failed_iteration: int | None = None  # the iteration of the failed node (with failed_node)
