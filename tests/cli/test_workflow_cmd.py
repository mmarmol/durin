"""Tests for `durin workflow` recommendations/apply/dismiss CLI.

Mirrors ``tests/cli/test_skill_cmd.py``: ``_workspace`` patched to a tmp path,
recommendations seeded through the real queue (``workflow_recommendations``) so
these exercise the same record shapes the CLI must render without crashing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from durin.cli.workflow_cmd import workflow_app
from durin.workflow import workflow_recommendations as wr

runner = CliRunner()


def _invoke(tmp_path: Path, *args: str):
    with patch("durin.cli.workflow_cmd._workspace", return_value=tmp_path):
        return runner.invoke(workflow_app, list(args))


def _mk_workflow(tmp_path: Path, name: str = "wf") -> None:
    from durin.workflow.loader import workflows_dir

    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name, "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "old prompt", "next": None}],
    }))


def test_recommendations_lists_script_file_rec_without_crashing(tmp_path: Path) -> None:
    wr.log_script_file_recommendation(
        tmp_path, "wf", script="repair.sh", current="old content",
        proposed="new content", reason="fixes a recurring crash",
    )
    result = _invoke(tmp_path, "recommendations", "wf")
    assert result.exit_code == 0
    assert "repair.sh" in result.stdout
    assert "fixes a recurring crash" in result.stdout


def test_recommendations_lists_script_file_manual_only_note(tmp_path: Path) -> None:
    wr.log_script_file_recommendation(
        tmp_path, "wf", script="gate.sh", current="old", proposed="new",
        reason="gate touch", manual_only=True,
    )
    result = _invoke(tmp_path, "recommendations", "wf")
    assert result.exit_code == 0
    assert "gate.sh" in result.stdout
    assert "manual_only" in result.stdout.lower()


def test_recommendations_lists_structural_rec_without_crashing(tmp_path: Path) -> None:
    wr.log_structural_suggestion(
        tmp_path, "wf", proposal={"field": "prompt", "target_id": "missing"},
        why_rejected="target 'missing' is not an editable node of this workflow",
        diagnostic="loop-backs {}",
    )
    result = _invoke(tmp_path, "recommendations", "wf")
    assert result.exit_code == 0
    assert "STRUCTURAL" in result.stdout


def test_recommendations_lists_prompt_rec_without_crashing(tmp_path: Path) -> None:
    wr.log_recommendation(
        tmp_path, "wf", target_id="a", field="prompt",
        current="old", proposed="new", reason="loops too often",
    )
    result = _invoke(tmp_path, "recommendations", "wf")
    assert result.exit_code == 0
    assert "a.prompt" in result.stdout


def test_apply_script_file_recommendation_prints_success(tmp_path: Path) -> None:
    from durin.workflow.loader import workflows_dir

    _mk_workflow(tmp_path)
    scripts_dir = workflows_dir(tmp_path) / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "repair.sh").write_text("#!/bin/bash\necho old\n")
    rid = wr.log_script_file_recommendation(
        tmp_path, "wf", script="repair.sh", current="#!/bin/bash\necho old\n",
        proposed="#!/bin/bash\necho new\n", reason="fixes a recurring crash",
    )
    result = _invoke(tmp_path, "apply", "wf", rid)
    assert result.exit_code == 0
    assert "repair.sh" in result.stdout
    assert (scripts_dir / "repair.sh").read_text() == "#!/bin/bash\necho new\n"


def test_apply_prompt_recommendation_prints_success(tmp_path: Path) -> None:
    _mk_workflow(tmp_path)
    rid = wr.log_recommendation(
        tmp_path, "wf", target_id="a", field="prompt",
        current="old prompt", proposed="new, sharper prompt", reason="a loops",
    )
    result = _invoke(tmp_path, "apply", "wf", rid)
    assert result.exit_code == 0
    assert "a.prompt" in result.stdout
