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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import durin.telemetry.logger as telemetry_logger
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
    "case-insensitive substring), an exact `status`, and/or a `since`/`until` "
    "date range on when the run started (ISO date or datetime; a bare "
    "`until` date reaches through the end of that day); results are "
    "newest first, capped at `limit` (default 10, max 50), one line each "
    "with run_id/workflow/status/started_at/duration, plus model, a short "
    "spec_hash, and a reused-node count when the manifest carries them. "
    "action=\"show\": full detail for one run by `run_id` — the same "
    "summary, each node's status/duration/model, the run's working "
    "folder path, and the artifact files recorded there (each with its "
    "producer model and date, or labeled \"unstamped\" when the file "
    "exists but was never recorded). "
    "action=\"cost\": the per-run token table — one line per node (LLM "
    "calls, prompt/fresh/output tokens, LLM minutes, dominant model), "
    "visits of the same node collapsed into one summed line, aggregated "
    "from this run's provider.call telemetry AND that of any child "
    "sub-workflow runs it launched (a run's cost includes its children), "
    "plus a TOTAL line and how many nodes were reused for free. States "
    "plainly when no telemetry is found for the run's date(s) instead of "
    "showing a table of zeros. "
    "When you answer from a prior run, state the run's date. If the "
    "producer's model or workflow version differs from the current "
    "configuration, say so. Prefer re-running when the user asked for an "
    "investigation; prefer reading when they asked a question about past "
    "work."
)

_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "What to do: search (find past runs by workflow name or task text, "
        "optionally filtered by status) | show (full detail for one run by "
        "id) | cost (per-run token/cost table for one run by id, including "
        "its child sub-workflow runs).",
        enum=["search", "show", "cost"],
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
    since=StringSchema(
        description=(
            "search only. Optional lower bound on when the run started — "
            "an ISO date ('2024-03-01') or datetime. Omit for no lower bound."
        ),
        nullable=True,
    ),
    until=StringSchema(
        description=(
            "search only. Optional upper bound on when the run started — "
            "an ISO date or datetime. A bare date means through the END of "
            "that day (inclusive), not its midnight. Omit for no upper bound."
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
        description=(
            "show | cost only. The run id to show or cost (from a prior "
            "search's [run_id])."
        ),
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


def _parse_date_bound(value: str, *, end_of_day: bool) -> float:
    """Parse a `since`/`until` bound (ISO date or datetime) to epoch seconds
    (UTC), tolerantly — via ``datetime.fromisoformat``.

    A bare ISO date (anything ``date.fromisoformat`` accepts, e.g.
    "2024-03-01") means midnight UTC at the START of that day, EXCEPT for
    the `until` bound (``end_of_day=True``), where a bare date means the
    LAST instant of that day instead — so ``until="2024-03-01"`` covers
    every run that started anywhere in that whole day, not just before its
    midnight. A full ISO datetime is used exactly as given; a naive one (no
    offset/Z) is treated as UTC, matching ``started_at``'s own epoch/UTC
    convention.

    Raises ``ValueError`` on anything unparseable — callers turn that into
    a clear, parameter-named error message instead of letting it propagate.
    """
    try:
        d = date.fromisoformat(value)
    except ValueError:
        dt = datetime.fromisoformat(value)  # raises ValueError on anything invalid
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if end_of_day:
        return datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=timezone.utc).timestamp()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


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
        run_id: str | None = None, since: str | None = None,
        until: str | None = None, **kwargs: Any,
    ) -> str:
        if action == "search":
            return self._search(query=query, status=status, limit=limit, since=since, until=until)
        if action == "show":
            if not run_id:
                return "Error: 'run_id' is required for show."
            return self._show(run_id)
        if action == "cost":
            if not run_id:
                return "Error: 'run_id' is required for cost."
            return self._cost(run_id)
        return f"Error: unknown action {action!r} (use search | show | cost)."

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

    def _search(
        self, *, query: str | None, status: str | None, limit: int | None,
        since: str | None = None, until: str | None = None,
    ) -> str:
        cap = _clamp_limit(limit)
        q = (query or "").strip().lower()
        status_filter = (status or "").strip() or None

        since_ts: float | None = None
        if since:
            try:
                since_ts = _parse_date_bound(since, end_of_day=False)
            except ValueError:
                return (
                    f"Error: invalid 'since' value {since!r} — expected an "
                    "ISO date (YYYY-MM-DD) or datetime."
                )
        until_ts: float | None = None
        if until:
            try:
                until_ts = _parse_date_bound(until, end_of_day=True)
            except ValueError:
                return (
                    f"Error: invalid 'until' value {until!r} — expected an "
                    "ISO date (YYYY-MM-DD) or datetime."
                )

        rows = []
        for rec in _iter_manifests(self._workspace):
            if status_filter is not None and rec.get("status") != status_filter:
                continue
            if q:
                workflow_name = (rec.get("workflow") or "").lower()
                task_text = (rec.get("task") or "").lower()
                if q not in workflow_name and q not in task_text:
                    continue
            if since_ts is not None or until_ts is not None:
                # started_at is always present on a real manifest; a record
                # missing or with an unparseable one can't be affirmed inside
                # the requested window, so it's excluded rather than guessed.
                try:
                    started_f = float(rec.get("started_at"))
                except (TypeError, ValueError):
                    continue
                if since_ts is not None and started_f < since_ts:
                    continue
                if until_ts is not None and started_f > until_ts:
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
            if since:
                filters.append(f"since={since!r}")
            if until:
                filters.append(f"until={until!r}")
            suffix = f" ({', '.join(filters)})" if filters else ""
            return f"No workflow runs found{suffix}."

        if total > cap:
            header = f"{total} workflow run(s) found — showing the {cap} most recent:"
        else:
            header = f"{total} workflow run(s) found:"
        lines = [header] + [self._summary_line(rec) for rec in shown]
        return "\n".join(lines)

    @staticmethod
    def _collapse_node_records(node_records: list) -> list[tuple[dict, int]]:
        """Group per-visit node records by node_id (first-seen order), keeping
        only the LATEST visit's fields plus how many visits it had.

        A manifest carries one record per node *visit*, not per node — a
        looping node (max_visits up to the low thousands) revisits the same
        node_id on every iteration, so the raw list can run far longer than
        the graph has nodes. Mirrors the exact collapse
        ``durin/agent/background_tasks.py``'s ``_node_tree`` already applies
        to this same shape, for the same reason (that one feeds the Work
        panel's node tree; this one feeds `show`'s per-node lines) — first
        occurrence fixes the display order, each later occurrence of the same
        node_id overwrites the stored entry so the latest status/duration/
        model/origin_run_id wins.
        """
        order: list[str] = []
        latest: dict[str, dict] = {}
        counts: dict[str, int] = {}
        for r in node_records:
            if not isinstance(r, dict):
                continue
            nid = r.get("node_id") or "?"
            if nid not in latest:
                order.append(nid)
            latest[nid] = r
            counts[nid] = counts.get(nid, 0) + 1
        return [(latest[nid], counts[nid]) for nid in order]

    @staticmethod
    def _node_line(r: dict, visits: int = 1) -> str:
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
        if visits > 1:
            bits.append(f"×{visits} visits")
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
            collapsed = self._collapse_node_records(node_records)
            lines.append("  nodes:")
            for r, visits in collapsed[:_MAX_ARTIFACTS]:
                lines.append(self._node_line(r, visits))
            if len(collapsed) > _MAX_ARTIFACTS:
                lines.append(f"    …and {len(collapsed) - _MAX_ARTIFACTS} more")

        work_dir = rec.get("work_dir") or str(
            Path(self._workspace) / ARTIFACT_ROOT / run_id / "work"
        )
        if rec.get("work_key"):
            lines.append(f"  work key: {rec['work_key']}")
        lines.append(f"  work dir: {work_dir}")
        lines.extend(self._artifact_lines(Path(work_dir)))
        return "\n".join(lines)

    @staticmethod
    def _descendant_manifests(workspace: str | Path, run_id: str) -> list[dict]:
        """Every manifest that is a descendant of *run_id* via ``parent_run_id``,
        at any depth. A subworkflow's child can itself launch a subworkflow, and
        each generation's ``parent_run_id`` only names its immediate parent, so a
        single pass would miss grandchildren — this walks the closure instead."""
        children_by_parent: dict[str, list[dict]] = {}
        for m in _iter_manifests(workspace):
            parent = m.get("parent_run_id")
            if parent:
                children_by_parent.setdefault(parent, []).append(m)
        out: list[dict] = []
        seen = {run_id}
        frontier = [run_id]
        while frontier:
            next_frontier: list[str] = []
            for rid in frontier:
                for child in children_by_parent.get(rid, []):
                    cid = child.get("run_id")
                    if cid and cid not in seen:
                        seen.add(cid)
                        out.append(child)
                        next_frontier.append(cid)
            frontier = next_frontier
        return out

    @staticmethod
    def _candidate_telemetry_dates(manifests: list[dict]) -> set[str]:
        """ISO dates a node visit's telemetry file could be stamped with: each
        manifest's started_at/finished_at date and today's, each ±1 day.

        Telemetry filenames carry the LOCAL calendar date at write time (see
        ``get_session_logger``'s ``date.today()``), which can land a day off
        from a naive read of started_at/finished_at (timezone rounding), and a
        run can itself span midnight — the buffer absorbs both without needing
        to know which."""
        dates: set[str] = set()
        timestamps: list[float] = [time.time()]
        for m in manifests:
            for key in ("started_at", "finished_at"):
                ts = m.get(key)
                if ts is not None:
                    timestamps.append(ts)
        for ts in timestamps:
            try:
                base = datetime.fromtimestamp(float(ts)).date()
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            for delta in (-1, 0, 1):
                dates.add((base + timedelta(days=delta)).isoformat())
        return dates

    @staticmethod
    def _sanitize_session_key(key: str) -> str:
        """Mirror ``get_session_logger``'s filename-safe encoding of a session
        key, without that function's directory-creating side effect — this
        tool is read-only and must never touch the telemetry directory."""
        safe = re.sub(r"[^\w\-]", "_", key)[:80]
        return re.sub(r"\.{2,}", "_", safe)

    def _cost(self, run_id: str) -> str:
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

        children = self._descendant_manifests(self._workspace, run_id)
        all_manifests = [rec] + children
        run_ids = [m.get("run_id") for m in all_manifests if m.get("run_id")]
        candidate_dates = self._candidate_telemetry_dates(all_manifests)

        # sanitized session_key -> (owning run_id, node_id): lets a matched
        # telemetry file be attributed back to the exact node visit that
        # produced it, rather than guessed from the sanitized filename alone
        # (a node_id can itself contain digits/underscores, which would make
        # splitting it back out of "workflow_<run>_<node>_<iter>" ambiguous).
        prefix_to_node: dict[str, tuple[str, str]] = {}
        for m in all_manifests:
            m_run_id = m.get("run_id") or "?"
            for r in (m.get("runs") or []):
                if not isinstance(r, dict):
                    continue
                sk = r.get("session_key")
                if sk:
                    prefix_to_node[self._sanitize_session_key(sk)] = (
                        m_run_id, r.get("node_id") or "?",
                    )

        tel_dir = telemetry_logger._DEFAULT_DIR
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        any_calls = False

        if tel_dir.is_dir():
            for f in tel_dir.glob("workflow_*.jsonl"):
                name = f.name
                # Anchored on the literal "workflow_<rid>_" prefix, not a bare
                # substring test — run ids are fixed-length hex today so a
                # containment check can't collide, but the id format is free to
                # grow a separator later, and an unanchored match would then
                # silently fold one run's telemetry into another's total.
                if not any(name.startswith(f"workflow_{rid}_") for rid in run_ids):
                    continue
                if not any(d in name for d in candidate_dates):
                    continue
                stem = name[: -len(".jsonl")]
                node_key = None
                for d in candidate_dates:
                    suffix = f"_{d}"
                    if stem.endswith(suffix):
                        node_key = prefix_to_node.get(stem[: -len(suffix)])
                        break
                if node_key is None:
                    node_key = ("?", "(unattributed)")
                try:
                    raw_lines = f.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line in raw_lines:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "provider.call":
                        continue
                    data = event.get("data") or {}
                    any_calls = True
                    b = buckets.setdefault(node_key, {
                        "calls": 0, "prompt": 0, "cached": 0, "out": 0,
                        "duration_ms": 0.0, "models": {},
                    })
                    b["calls"] += 1
                    b["prompt"] += int(data.get("prompt_tokens", 0) or 0)
                    b["cached"] += int(data.get("cached_tokens", 0) or 0)
                    b["out"] += int(data.get("completion_tokens", 0) or 0)
                    b["duration_ms"] += float(data.get("duration_ms", 0) or 0)
                    model = data.get("model")
                    if model:
                        b["models"][model] = b["models"].get(model, 0) + 1

        reused_total = sum(_reused_count(m.get("runs") or []) for m in all_manifests)

        if not any_calls:
            lines = [
                self._summary_line(rec),
                "No provider.call telemetry found for this run's date(s).",
            ]
            if reused_total:
                lines.append(f"  reused nodes: {reused_total} — they cost 0")
            return "\n".join(lines)

        rows = []
        for (node_run_id, node_id), b in buckets.items():
            label = node_id if node_run_id == run_id else f"{node_id}@{node_run_id}"
            dominant_model = max(b["models"], key=b["models"].get) if b["models"] else "?"
            fresh = b["prompt"] - b["cached"]
            llm_minutes = b["duration_ms"] / 1000.0 / 60.0
            rows.append({
                "label": label, "calls": b["calls"], "prompt": b["prompt"],
                "fresh": fresh, "out": b["out"], "llm_minutes": llm_minutes,
                "model": dominant_model,
            })
        rows.sort(key=lambda r: (-r["prompt"], r["label"]))

        lines = [self._summary_line(rec)]
        if children:
            lines.append(f"  cost includes {len(children)} child run(s)")
        shown = rows[:_MAX_ARTIFACTS]
        for r in shown:
            lines.append(
                f"  {r['label']} calls={r['calls']} prompt={r['prompt']} "
                f"fresh={r['fresh']} out={r['out']} llm={r['llm_minutes']:.1f}m "
                f"model={r['model']}"
            )
        if len(rows) > _MAX_ARTIFACTS:
            lines.append(f"  …and {len(rows) - _MAX_ARTIFACTS} more")

        total_calls = sum(r["calls"] for r in rows)
        total_prompt = sum(r["prompt"] for r in rows)
        total_fresh = sum(r["fresh"] for r in rows)
        total_out = sum(r["out"] for r in rows)
        total_llm = sum(r["llm_minutes"] for r in rows)
        lines.append(
            f"TOTAL: calls={total_calls} prompt={total_prompt} fresh={total_fresh} "
            f"out={total_out} llm={total_llm:.1f}m — reused nodes: {reused_total} "
            "— they cost 0"
        )
        return "\n".join(lines)
