"""Tests for the workflow_runs tool — read-only search over past workflow runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.agent.tools.workflow_runs import WorkflowRunsTool
from durin.workflow import provenance

T0 = 1_700_000_000.0

RUN_MODERN = "aaaaaa111111"   # completed, provenance fields + a reused node
RUN_LEGACY = "bbbbbb222222"   # completed, pre-provenance shape
RUN_ABORTED = "cccccc333333"  # aborted, different workflow name
RUN_LOOP = "dddddd444444"     # completed, one node revisited 40 times
RUN_WIDE = "eeeeee555555"     # completed, 35 distinct nodes, none repeated


def _write_manifest(workspace: Path, workflow: str, run_id: str, data: dict) -> None:
    d = workspace / "workflows-runs" / workflow
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    work_dir_modern = tmp_path / ".workflow" / RUN_MODERN / "work"
    work_dir_modern.mkdir(parents=True)

    # Modern manifest: real box shape post artifact-provenance (#538) and
    # reuse (#539) — top-level spec_hash/durin_version, per-node
    # model/provider/node_hash, and a "reused" node carrying origin_run_id.
    _write_manifest(tmp_path, "diagnose-ticket", RUN_MODERN, {
        "schema": 2, "run_id": RUN_MODERN, "workflow": "diagnose-ticket",
        "status": "completed", "root_session_key": "core:s1",
        "started_at": T0 + 200, "finished_at": T0 + 200 + 92 * 60,
        "ts": T0 + 200 + 92 * 60,
        "task": "Diagnose ticket 23124: customer reports gateway timeout under load.",
        "parent_run_id": None, "work_dir": str(work_dir_modern),
        "typical_s": {}, "typical_total_s": None,
        "spec_hash": "abcdef1234567890fedcba", "durin_version": "0.8.0",
        "final_output": "Root cause: connection pool exhaustion.",
        "final_output_node": "diagnose", "needs_input_node": None,
        "failed_node": None, "output_files": [], "missing_artifacts": [],
        "runs": [
            {
                "node_id": "fetch", "iteration": 1, "passed": True,
                "session_key": "sk1", "status": "ok", "duration_s": 12.0,
                "artifacts": ["ticket.json"], "model": "claude-sonnet-5",
                "provider": "anthropic", "node_hash": "h1", "origin_run_id": None,
            },
            {
                "node_id": "diagnose", "iteration": 1, "passed": True,
                "session_key": "sk2", "status": "reused", "duration_s": 0.0,
                "artifacts": ["diagnosis.md"], "model": "claude-sonnet-5",
                "provider": "anthropic", "node_hash": "h2",
                "origin_run_id": "origin000001",
            },
        ],
    })

    # Legacy manifest: schema 2 (started_at/task/work_dir already existed),
    # but written before the provenance PR — no spec_hash/durin_version, and
    # node records carry none of model/provider/node_hash/origin_run_id.
    _write_manifest(tmp_path, "diagnose-ticket", RUN_LEGACY, {
        "schema": 2, "run_id": RUN_LEGACY, "workflow": "diagnose-ticket",
        "status": "completed", "root_session_key": "core:s0",
        "started_at": T0 + 100, "finished_at": T0 + 100 + 600, "ts": T0 + 100 + 600,
        "task": "Diagnose ticket 23099: similar timeout reported last week.",
        "parent_run_id": None, "work_dir": None,
        "typical_s": {}, "typical_total_s": None,
        "final_output": "Root cause: same as 23124.",
        "final_output_node": "diagnose", "needs_input_node": None,
        "failed_node": None, "output_files": [], "missing_artifacts": [],
        "runs": [
            {"node_id": "fetch", "iteration": 1, "passed": True,
             "session_key": "sk3", "status": "ok", "duration_s": 8.0, "artifacts": []},
            {"node_id": "diagnose", "iteration": 1, "passed": True,
             "session_key": "sk4", "status": "ok", "duration_s": 300.0, "artifacts": []},
        ],
    })

    # Aborted manifest, different workflow — newest of the three, so it must
    # sort first when nothing filters it out.
    _write_manifest(tmp_path, "other-workflow", RUN_ABORTED, {
        "schema": 2, "run_id": RUN_ABORTED, "workflow": "other-workflow",
        "status": "aborted", "root_session_key": "core:s2",
        "started_at": T0 + 300, "finished_at": T0 + 345, "ts": T0 + 345,
        "task": "Investigate unrelated ticket 99999.",
        "parent_run_id": None, "work_dir": None,
        "typical_s": {}, "typical_total_s": None,
        "spec_hash": "1111222233334444", "durin_version": "0.8.0",
        "final_output": "", "failed_node": "fetch",
        "output_files": [], "missing_artifacts": [],
        "runs": [
            {"node_id": "fetch", "iteration": 1, "passed": False,
             "session_key": "sk5", "status": "aborted", "duration_s": 3.0,
             "model": "claude-sonnet-5", "provider": "anthropic",
             "node_hash": "h9", "origin_run_id": None, "error": "connection refused"},
        ],
    })

    # Work dir for the modern run: two files stamped in .provenance.json
    # (written via the real writer) plus one plain file the ledger never saw.
    provenance.record(work_dir_modern, "ticket.json", {
        "run_id": RUN_MODERN, "workflow": "diagnose-ticket", "node_id": "fetch",
        "iteration": 1, "finished_at": T0 + 205, "node_hash": "h1",
        "model": "claude-sonnet-5", "provider": "anthropic", "params_hash": "p1",
        "durin_version": "0.8.0",
    })
    provenance.record(work_dir_modern, "diagnosis.md", {
        "run_id": "origin000001", "workflow": "diagnose-ticket", "node_id": "diagnose",
        "iteration": 1, "finished_at": T0 + 210, "node_hash": "h2",
        "model": "claude-sonnet-5", "provider": "anthropic", "params_hash": "p2",
        "durin_version": "0.8.0",
    })
    (work_dir_modern / "ticket.json").write_text("{}", encoding="utf-8")
    (work_dir_modern / "diagnosis.md").write_text("# diagnosis", encoding="utf-8")
    (work_dir_modern / "notes.txt").write_text("scratch notes", encoding="utf-8")

    return tmp_path


@pytest.fixture
def tool(workspace: Path) -> WorkflowRunsTool:
    return WorkflowRunsTool(workspace=str(workspace))


@pytest.fixture
def loop_workspace(tmp_path: Path) -> Path:
    """A separate, isolated workspace (deliberately NOT folded into the shared
    `workspace` fixture, so this large manifest never perturbs the other
    tests' exact-count / newest-first assertions): one run whose node list
    interleaves 40 visits of a single looping node between two ordinary
    nodes — a real manifest shape once a loop node's max_visits is set high."""
    runs = [{"node_id": "start", "iteration": 1, "status": "ok", "duration_s": 2.0}]
    for i in range(1, 40):
        runs.append({
            "node_id": "loop_node", "iteration": i, "status": "ok",
            "duration_s": 5.0, "model": "claude-sonnet-5",
        })
    # The 40th (last) visit carries different fields from the prior 39, so a
    # test can prove the collapsed line reports the LATEST visit, not the first.
    runs.append({
        "node_id": "loop_node", "iteration": 40, "status": "ok",
        "duration_s": 123.0, "model": "claude-opus-5",
    })
    runs.append({"node_id": "finish", "iteration": 1, "status": "ok", "duration_s": 3.0})
    _write_manifest(tmp_path, "loop-workflow", RUN_LOOP, {
        "schema": 2, "run_id": RUN_LOOP, "workflow": "loop-workflow",
        "status": "completed", "started_at": T0, "finished_at": T0 + 600,
        "ts": T0 + 600, "task": "loop node visit collapsing", "work_dir": None,
        "runs": runs,
    })
    return tmp_path


@pytest.fixture
def wide_workspace(tmp_path: Path) -> Path:
    """A separate, isolated workspace (same isolation rationale as
    `loop_workspace`): one run with 35 DISTINCT nodes, none repeated — exercises
    the artifact-style cap on the collapsed (not raw) node count."""
    runs = [
        {"node_id": f"n{i:02d}", "iteration": 1, "status": "ok", "duration_s": 1.0}
        for i in range(1, 36)
    ]
    _write_manifest(tmp_path, "wide-workflow", RUN_WIDE, {
        "schema": 2, "run_id": RUN_WIDE, "workflow": "wide-workflow",
        "status": "completed", "started_at": T0, "finished_at": T0 + 60,
        "ts": T0 + 60, "task": "35 distinct nodes", "work_dir": None,
        "runs": runs,
    })
    return tmp_path


# --- search -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_matches_task_text_case_insensitively(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", query="23124")
    assert RUN_MODERN in out
    assert RUN_LEGACY not in out
    assert RUN_ABORTED not in out


@pytest.mark.asyncio
async def test_search_matches_workflow_name_case_insensitively(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", query="DIAGNOSE-TICKET")
    assert RUN_MODERN in out
    assert RUN_LEGACY in out
    assert RUN_ABORTED not in out


@pytest.mark.asyncio
async def test_search_reports_provenance_fields_on_modern_row(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", query="23124")
    assert "model=claude-sonnet-5" in out
    assert "spec=abcdef12" in out          # spec_hash truncated to 8 chars
    assert "reused: 1 nodes" in out        # one node has status "reused"
    assert "92m" in out                    # 5520s = 92 minutes, no decimal


@pytest.mark.asyncio
async def test_results_order_newest_first(tool: WorkflowRunsTool):
    out = await tool.execute(action="search")
    assert out.index(RUN_ABORTED) < out.index(RUN_MODERN) < out.index(RUN_LEGACY)


@pytest.mark.asyncio
async def test_limit_respected(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", limit=1)
    assert RUN_ABORTED in out          # newest
    assert RUN_MODERN not in out
    assert RUN_LEGACY not in out
    assert "showing the 1 most recent" in out


@pytest.mark.asyncio
async def test_search_limit_zero_floors_to_one_result(tool: WorkflowRunsTool):
    # limit=0 must never mean "no results" -- clamped to the floor of 1.
    out = await tool.execute(action="search", limit=0)
    assert "showing the 1 most recent" in out
    assert RUN_ABORTED in out          # newest -- the one result shown
    assert RUN_MODERN not in out
    assert RUN_LEGACY not in out


def test_clamp_limit_over_max_caps_at_fifty():
    from durin.agent.tools.workflow_runs import _clamp_limit
    assert _clamp_limit(100) == 50


def test_clamp_limit_zero_floors_at_one():
    from durin.agent.tools.workflow_runs import _clamp_limit
    assert _clamp_limit(0) == 1


@pytest.mark.asyncio
async def test_status_filter(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", status="aborted")
    assert RUN_ABORTED in out
    assert RUN_MODERN not in out
    assert RUN_LEGACY not in out


@pytest.mark.asyncio
async def test_legacy_manifest_listed_without_crash_or_invented_fields(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", query="23099")
    assert RUN_LEGACY in out
    assert "model=" not in out
    assert "spec=" not in out
    assert "reused" not in out


@pytest.mark.asyncio
async def test_search_no_matches(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", query="no-such-ticket-anywhere")
    assert "No workflow runs found" in out


# --- search: since/until date filtering ----------------------------------
# All three fixture runs started on 2023-11-14 UTC: RUN_LEGACY at 22:15:00
# (T0+100), RUN_MODERN at 22:16:40 (T0+200), RUN_ABORTED at 22:18:20 (T0+300).


@pytest.mark.asyncio
async def test_search_since_excludes_older_runs(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", since="2023-11-14T22:16:00")
    assert RUN_LEGACY not in out           # started before the bound
    assert RUN_MODERN in out               # started after
    assert RUN_ABORTED in out              # started after


@pytest.mark.asyncio
async def test_search_until_date_only_includes_same_day(tool: WorkflowRunsTool):
    # A date-only `until` must be inclusive of the WHOLE day, not just up to
    # midnight at its start -- every fixture run started later that same day.
    out = await tool.execute(action="search", until="2023-11-14")
    assert RUN_LEGACY in out
    assert RUN_MODERN in out
    assert RUN_ABORTED in out


@pytest.mark.asyncio
async def test_search_until_date_only_excludes_next_day(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", until="2023-11-13")
    assert "No workflow runs found" in out


@pytest.mark.asyncio
async def test_search_since_and_until_combine(tool: WorkflowRunsTool):
    out = await tool.execute(
        action="search", since="2023-11-14T22:16:00", until="2023-11-14T22:17:00",
    )
    assert RUN_MODERN in out
    assert RUN_LEGACY not in out
    assert RUN_ABORTED not in out


@pytest.mark.asyncio
async def test_search_date_filter_includes_legacy_manifest_in_range(tool: WorkflowRunsTool):
    # RUN_LEGACY predates artifact-provenance (no spec_hash/durin_version) but
    # always carries started_at -- the date filter must not treat it specially.
    out = await tool.execute(
        action="search", since="2023-11-14T22:14:00", until="2023-11-14T22:15:30",
    )
    assert RUN_LEGACY in out
    assert RUN_MODERN not in out
    assert RUN_ABORTED not in out


@pytest.mark.asyncio
async def test_search_invalid_since_is_a_clear_error(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", since="not-a-date")
    assert "Error" in out
    assert "since" in out


@pytest.mark.asyncio
async def test_search_invalid_until_is_a_clear_error(tool: WorkflowRunsTool):
    out = await tool.execute(action="search", until="also-not-a-date")
    assert "Error" in out
    assert "until" in out


# --- show ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_returns_manifest_and_artifact_paths(tool: WorkflowRunsTool):
    out = await tool.execute(action="show", run_id=RUN_MODERN)
    assert RUN_MODERN in out
    assert "work dir:" in out
    assert "ticket.json" in out
    assert "diagnosis.md" in out


@pytest.mark.asyncio
async def test_show_renders_producer_fields_and_origin_run_id(tool: WorkflowRunsTool):
    out = await tool.execute(action="show", run_id=RUN_MODERN)
    assert "model=claude-sonnet-5" in out
    assert "spec=abcdef12" in out
    assert "origin_run_id=origin000001" in out


@pytest.mark.asyncio
async def test_show_labels_unstamped_artifact(tool: WorkflowRunsTool):
    out = await tool.execute(action="show", run_id=RUN_MODERN)
    assert "unstamped files in work dir" in out
    assert "notes.txt" in out
    # the provenance ledger itself must never be listed as an artifact entry
    # (the header text legitimately names the file in prose, so check for it
    # as a bullet line specifically, not as a bare substring)
    assert "- .provenance.json" not in out


@pytest.mark.asyncio
async def test_show_stamped_artifacts_carry_model_and_date(tool: WorkflowRunsTool):
    out = await tool.execute(action="show", run_id=RUN_MODERN)
    # finished_at T0+205 -> 2023-11-14 (UTC date of the fixture timestamp)
    assert "produced=" in out


@pytest.mark.asyncio
async def test_show_legacy_manifest_no_crash_no_invented_fields(tool: WorkflowRunsTool):
    out = await tool.execute(action="show", run_id=RUN_LEGACY)
    assert RUN_LEGACY in out
    assert "model=" not in out
    assert "spec=" not in out


@pytest.mark.asyncio
async def test_show_renders_work_key_when_present(tool: WorkflowRunsTool, workspace: Path):
    _write_manifest(workspace, "diagnose-ticket", "ffffff666666", {
        "schema": 2, "run_id": "ffffff666666", "workflow": "diagnose-ticket",
        "status": "completed", "root_session_key": None,
        "started_at": T0, "finished_at": T0 + 10, "ts": T0 + 10,
        "task": "t", "parent_run_id": None, "work_dir": None, "work_key": "ticket-23124",
        "typical_s": {}, "typical_total_s": None, "spec_hash": None, "durin_version": None,
        "final_output": "done", "final_output_node": None, "needs_input_node": None,
        "failed_node": None, "output_files": [], "missing_artifacts": [], "runs": [],
    })
    out = await tool.execute(action="show", run_id="ffffff666666")
    assert "work key: ticket-23124" in out


@pytest.mark.asyncio
async def test_show_omits_work_key_line_when_absent(tool: WorkflowRunsTool):
    out = await tool.execute(action="show", run_id=RUN_MODERN)
    assert "work key:" not in out


@pytest.mark.asyncio
async def test_show_run_id_validation_rejects_path_traversal(tool: WorkflowRunsTool):
    out = await tool.execute(action="show", run_id="../evil")
    assert "Error" in out
    assert "not a valid run id" in out


@pytest.mark.asyncio
async def test_show_unknown_run_id_returns_not_found_text(tool: WorkflowRunsTool):
    out = await tool.execute(action="show", run_id="deadbeef0000")
    assert "No workflow run found" in out


@pytest.mark.asyncio
async def test_show_requires_run_id(tool: WorkflowRunsTool):
    out = await tool.execute(action="show")
    assert "Error" in out
    assert "run_id" in out


@pytest.mark.asyncio
async def test_show_collapses_looping_node_visits(loop_workspace: Path):
    tool = WorkflowRunsTool(workspace=str(loop_workspace))
    out = await tool.execute(action="show", run_id=RUN_LOOP)
    node_lines = [ln for ln in out.splitlines() if ln.strip().startswith("- ")]
    # 3 distinct node_ids (start, loop_node, finish) -- not 41 raw visit rows.
    assert len(node_lines) == 3
    loop_line = next(ln for ln in node_lines if "loop_node" in ln)
    assert "×40 visits" in loop_line
    # The collapsed line reports the LAST visit's fields, not an earlier one --
    # checked on the node line specifically: the top summary line legitimately
    # shows "claude-sonnet-5" too (ruling 2's "first node record carrying one"
    # rule picks it from an early loop_node visit, since "start" has no model).
    assert "model=claude-opus-5" in loop_line
    assert "model=claude-sonnet-5" not in loop_line


@pytest.mark.asyncio
async def test_show_caps_distinct_nodes_with_overflow(wide_workspace: Path):
    tool = WorkflowRunsTool(workspace=str(wide_workspace))
    out = await tool.execute(action="show", run_id=RUN_WIDE)
    node_lines = [ln for ln in out.splitlines() if ln.strip().startswith("- ")]
    assert len(node_lines) == 30
    assert "…and 5 more" in out


@pytest.mark.asyncio
async def test_unknown_action_is_a_clear_error(tool: WorkflowRunsTool):
    out = await tool.execute(action="delete", run_id=RUN_MODERN)
    assert "Error" in out
    assert "search | show" in out


# --- gating -------------------------------------------------------------


def test_enabled_mirrors_tasks_tool_gate():
    from types import SimpleNamespace

    assert WorkflowRunsTool.enabled(SimpleNamespace(workspace="/tmp/somewhere")) is True
    assert WorkflowRunsTool.enabled(SimpleNamespace()) is False


def test_tool_is_in_plan_mode_allowed_set():
    # Read-only w.r.t. the workspace, like its peers tasks/list_workflows/session_search.
    from durin.agent.agent_mode import PLAN_MODE
    assert PLAN_MODE.is_tool_allowed("workflow_runs")


def test_read_only_and_core_scope():
    assert WorkflowRunsTool(workspace="/tmp").read_only is True
    assert WorkflowRunsTool._scopes == {"core"}
