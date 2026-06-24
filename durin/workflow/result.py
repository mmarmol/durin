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
    passed: bool | None = None       # for a routing node: did its verdict pass? (None = not a routing node)


@dataclass
class WorkflowResult:
    status: Literal["completed", "max_visits", "aborted"]
    final_output: str | None
    runs: list[NodeRun] = field(default_factory=list)
    run_id: str = ""
    output_dir: str | None = None  # the terminal node's output folder, if any
