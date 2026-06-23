"""Load a workflow definition from disk by name.

Workflow JSON files live under ``<workspace>/workflows/<name>.json``. This loads
one, parses it via the spec, and returns a validated Workflow. A missing file
raises WorkflowNotFound; a malformed definition raises WorkflowError (from the
parser).
"""

from __future__ import annotations

import json
from pathlib import Path

from durin.workflow.spec import Workflow, WorkflowError, parse_workflow


class WorkflowNotFound(WorkflowError):
    """Raised when no workflow JSON exists for the requested name."""


def workflows_dir(workspace: str | Path) -> Path:
    """The directory holding workflow definitions for a workspace."""
    return Path(workspace) / "workflows"


def load_workflow(workspace: str | Path, name: str) -> Workflow:
    """Load and parse ``<workspace>/workflows/<name>.json``."""
    path = workflows_dir(workspace) / f"{name}.json"
    if not path.is_file():
        raise WorkflowNotFound(f"no workflow named {name!r} at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return parse_workflow(data)
