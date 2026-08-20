"""``workflow_runs`` tool — read-only search over past workflow-run manifests.

The agent already has `tasks` for the runs IT launched this session, and
`run_workflow` to launch a new one. Neither answers "has a workflow already
diagnosed this?" — `tasks` only sees the current session's launches, and the
only way to reuse a past finding was to re-run the workflow from scratch
(expensive: a multi-node investigation can cost millions of tokens to redo).
This tool searches every run manifest ever recorded in the workspace
(`<workspace>/workflows-runs/<name>/<run_id>.json`, written by
`durin/workflow/run_log.py`), across every session, so the agent can read a
prior finding instead of reproducing it.

It is deliberately thin: `search` and `show` return identifying summaries and
file PATHS, never artifact contents — the agent reads those with its existing
`read_file` tool, keeping this tool's own results small. A manifest recorded
before the artifact-provenance work (spec_hash, durin_version, per-node
model/provider/node_hash, reuse's origin_run_id) simply omits those fields
rather than erroring — old runs must stay discoverable.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from durin.workflow import provenance, run_log
from durin.workflow.artifacts import ARTIFACT_ROOT

# A run_id is minted as ``uuid.uuid4().hex[:12]`` (see engine.py / run_workflow.py) —
# lowercase hex. The shape check is intentionally a bit looser (hyphen allowed, 6+
# chars) so it never has to change if the id format grows a separator later, while
# still rejecting anything that could touch a path outside the manifest tree
# (no ``.``, ``/``, or other traversal characters are in the allowed set).
_RUN_ID_RE = re.compile(r"^[a-z0-9-]{6,}$")

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50
_MAX_ARTIFACTS = 30

_DESCRIPTION = (
    "Consult past workflow runs recorded in this workspace — across every "
    "session, not just this one — instead of re-running a workflow to "
    "answer a question about work already done. "
    "action=\"search\": find runs by workflow name or task text (`query`, "
    "case-insensitive substring) and/or an exact `status`; results are "
    "newest first, capped at `limit` (default 10, max 50), one line each "
    "with run_id/workflow/status/started_at/duration, plus model, a short "
    "spec_hash, and a reused-node count when the manifest carries them. "
    "action=\"show\": full detail for one run by `run_id` — the same "
    "summary, each node's status/duration/model, the run's working "
    "folder path, and the artifact files recorded there (each with its "
    "producer model and date, or labeled \"unstamped\" when the file "
    "exists but was never recorded). "
    "When you answer from a prior run, state the run's date. If the "
    "producer's model or workflow version differs from the current "
    "configuration, say so. Prefer re-running when the user asked for an "
    "investigation; prefer reading when they asked a question about past "
    "work."
)

_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "What to do: search (find past runs by workflow name or task text, "
        "optionally filtered by status) | show (full detail for one run by id).",
        enum=["search", "show"],
    ),
    query=StringSchema(
        description=(
            "search only. Optional substring, matched case-insensitively "
            "against the workflow name and the task text. Omit to list the "
            "most recent runs across every workflow."
        ),
        nullable=True,
    ),
    status=StringSchema(
        description=(
            "search only. Optional exact status filter — e.g. 'completed', "
            "'aborted', 'cancelled', 'crashed', 'running', 'needs_input', "
            "'exhausted'. Omit to match any status."
        ),
        nullable=True,
    ),
    limit=IntegerSchema(
        description="search only. Maximum rows to return, newest first. Default 10, max 50.",
        minimum=1,
        maximum=_MAX_LIMIT,
        nullable=True,
    ),
    run_id=StringSchema(
        description="show only. The run id to show (from a prior search's [run_id]).",
        nullable=True,
    ),
    required=["action"],
)


def _human_duration(seconds: float) -> str:
    # Seconds below a minute, otherwise whole minutes, uncapped — a workflow
    # run is minutes-to-low-hours in practice, and rendering those as e.g.
    # "180m" stays unambiguous while keeping one unit instead of a threshold
    # nobody specified for when minutes should roll over into hours.
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f}m"


def _duration_for_rec(rec: dict) -> str | None:
    started = rec.get("started_at")
    if started is None:
        return None
    finished = rec.get("finished_at")
    end = finished if finished is not None else time.time()
    try:
        return _human_duration(float(end) - float(started))
    except (TypeError, ValueError):
        return None


def _started_at_iso(rec: dict) -> str | None:
    # Genuinely ancient (pre-schema-versioning) manifests have no started_at at
    # all — only the cursor field `ts`, which finalize_run sets equal to
    # finished_at. Falling back to it still gives a usable (if approximate) date
    # instead of silently dropping the run's timing from the row entirely.
    ts = rec.get("started_at")
    if ts is None:
        ts = rec.get("ts")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="minutes")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _first_model(node_records: list) -> str | None:
    """The model of the first node record that names one — a run-level
    summary field; a run can touch several models, but this is enough to
    flag "does the producer match what's configured now"."""
    for r in node_records:
        if isinstance(r, dict):
            model = r.get("model")
            if model:
                return model
    return None


def _reused_count(node_records: list) -> int:
    return sum(1 for r in node_records if isinstance(r, dict) and r.get("status") == "reused")


def _iter_manifests(workspace: str | Path):
    """Every raw manifest dict under ``workflows-runs/*/*.json``, unordered.

    Mirrors the glob-and-skip-``.cursor.json``-and-skip-malformed pattern
    ``run_log`` itself uses internally (``list_all_runs``, ``reconcile_running``)
    — there is no public reader that returns FULL manifests in bulk (the public
    list functions intentionally return trimmed summaries), so this tool reads
    the same files those functions do, the same defensive way.
    """
    root = run_log.runs_root(workspace)
    if not root.is_dir():
        return
    for f in root.glob("*/*.json"):
        if f.name == ".cursor.json":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict):
            yield rec


def _clamp_limit(limit: int | None) -> int:
    cap = _DEFAULT_LIMIT if limit is None else limit
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = _DEFAULT_LIMIT
    return max(1, min(cap, _MAX_LIMIT))


@tool_parameters(_PARAMETERS)
class WorkflowRunsTool(Tool):
    """Read-only search over past workflow-run manifests recorded in this workspace."""

    # Core-only, like `tasks`: this reads across the whole workspace's run
    # history, not scoped to a session, but a background sub-agent has no need
    # to browse prior investigations — its job is the one task it was spawned
    # for. Explicit (rather than relying on the base default) so the choice is
    # grep-able, matching how tasks_tool documents the same call.
    _scopes = {"core"}

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "workflow_runs"

    @property
    def description(self) -> str:
        return _DESCRIPTION

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        # Same gate as TasksTool: always on wherever a workspace exists, no
        # config flag — a run history is workspace-inherent, not opt-in.
        return getattr(ctx, "workspace", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> "WorkflowRunsTool":
        return cls(workspace=ctx.workspace)

    async def execute(  # type: ignore[override]
        self, action: str | None = None, query: str | None = None,
        status: str | None = None, limit: int | None = None,
        run_id: str | None = None, **kwargs: Any,
    ) -> str:
        if action == "search":
            return self._search(query=query, status=status, limit=limit)
        if action == "show":
            if not run_id:
                return "Error: 'run_id' is required for show."
            return self._show(run_id)
        return f"Error: unknown action {action!r} (use search | show)."

    def _summary_line(self, rec: dict) -> str:
        """The one-line summary shared by a search row and show's header —
        same fields, same order, so the two actions never drift apart."""
        run_id = rec.get("run_id") or "?"
        workflow = rec.get("workflow") or "?"
        status = rec.get("status") or "?"
        bits = [f"[{run_id}]", workflow, status]

        started = _started_at_iso(rec)
        if started:
            bits.append(f"started={started}")
        duration = _duration_for_rec(rec)
        if duration:
            bits.append(f"duration={duration}")

        node_records = rec.get("runs") or []
        model = _first_model(node_records)
        if model:
            bits.append(f"model={model}")
        spec_hash = rec.get("spec_hash")
        if spec_hash:
            bits.append(f"spec={spec_hash[:8]}")
        reused_n = _reused_count(node_records)
        if reused_n:
            bits.append(f"reused: {reused_n} nodes")
        return "  " + " ".join(bits)

    def _search(self, *, query: str | None, status: str | None, limit: int | None) -> str:
        cap = _clamp_limit(limit)
        q = (query or "").strip().lower()
        status_filter = (status or "").strip() or None

        rows = []
        for rec in _iter_manifests(self._workspace):
            if status_filter is not None and rec.get("status") != status_filter:
                continue
            if q:
                workflow_name = (rec.get("workflow") or "").lower()
                task_text = (rec.get("task") or "").lower()
                if q not in workflow_name and q not in task_text:
                    continue
            rows.append(rec)

        rows.sort(key=lambda r: r.get("started_at") or r.get("ts") or 0.0, reverse=True)
        total = len(rows)
        shown = rows[:cap]

        if not shown:
            filters = []
            if q:
                filters.append(f"query={query!r}")
            if status_filter:
                filters.append(f"status={status_filter!r}")
            suffix = f" ({', '.join(filters)})" if filters else ""
            return f"No workflow runs found{suffix}."

        if total > cap:
            header = f"{total} workflow run(s) found — showing the {cap} most recent:"
        else:
            header = f"{total} workflow run(s) found:"
        lines = [header] + [self._summary_line(rec) for rec in shown]
        return "\n".join(lines)

    @staticmethod
    def _node_line(r: dict) -> str:
        node_id = r.get("node_id") or "?"
        status = r.get("status") or "?"
        bits = [f"    - {node_id} [{status}]"]
        duration_s = r.get("duration_s")
        if duration_s is not None:
            try:
                bits.append(_human_duration(float(duration_s)))
            except (TypeError, ValueError):
                pass
        model = r.get("model")
        if model:
            bits.append(f"model={model}")
        if status == "reused":
            origin = r.get("origin_run_id")
            if origin:
                bits.append(f"origin_run_id={origin}")
        return " ".join(bits)

    @staticmethod
    def _artifact_lines(work_dir: Path) -> list[str]:
        if not work_dir.is_dir():
            return ["  (work dir not found on disk)"]

        prov = provenance.load(work_dir)
        stamped = sorted(prov.items())
        out: list[str] = []
        out.append(
            f"  artifacts ({len(stamped)} recorded in .provenance.json):"
            if stamped else "  artifacts: none recorded in .provenance.json"
        )
        for filename, entry in stamped[:_MAX_ARTIFACTS]:
            if not isinstance(entry, dict):
                out.append(f"    - {filename}")
                continue
            bits = [f"    - {filename}"]
            model = entry.get("model")
            if model:
                bits.append(f"model={model}")
            finished_at = entry.get("finished_at")
            if finished_at is not None:
                try:
                    when = datetime.fromtimestamp(
                        float(finished_at), tz=timezone.utc).date().isoformat()
                    bits.append(f"produced={when}")
                except (TypeError, ValueError, OSError, OverflowError):
                    pass
            out.append(" ".join(bits))
        if len(stamped) > _MAX_ARTIFACTS:
            out.append(f"    …and {len(stamped) - _MAX_ARTIFACTS} more")

        try:
            all_files = sorted(p for p in work_dir.rglob("*") if p.is_file())
        except OSError:
            all_files = []
        prov_keys = set(prov.keys())
        unstamped = [
            rel for p in all_files
            if p.name != provenance.FILENAME
            and (rel := str(p.relative_to(work_dir))) not in prov_keys
        ]
        if unstamped:
            out.append(f"  unstamped files in work dir ({len(unstamped)}):")
            for rel in unstamped[:_MAX_ARTIFACTS]:
                out.append(f"    - {rel}")
            if len(unstamped) > _MAX_ARTIFACTS:
                out.append(f"    …and {len(unstamped) - _MAX_ARTIFACTS} more")
        return out

    def _show(self, run_id: str) -> str:
        if not _RUN_ID_RE.match(run_id):
            return (
                f"Error: {run_id!r} is not a valid run id (expected lowercase "
                "letters, digits, and hyphens, at least 6 characters)."
            )
        root = run_log.runs_root(self._workspace)
        match = next(root.glob(f"*/{run_id}.json"), None) if root.is_dir() else None
        if match is None:
            return f"No workflow run found with id {run_id!r}."
        rec = run_log.read_manifest(self._workspace, match.parent.name, run_id)
        if rec is None:
            return f"No workflow run found with id {run_id!r}."

        lines = [self._summary_line(rec)]
        node_records = rec.get("runs") or []
        if node_records:
            lines.append("  nodes:")
            lines.extend(self._node_line(r) for r in node_records if isinstance(r, dict))

        work_dir = rec.get("work_dir") or str(
            Path(self._workspace) / ARTIFACT_ROOT / run_id / "work"
        )
        lines.append(f"  work dir: {work_dir}")
        lines.extend(self._artifact_lines(Path(work_dir)))
        return "\n".join(lines)
