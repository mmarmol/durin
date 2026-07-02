"""Tests for the bundled seed workflow JSONs and the seed_workflows seeding function."""

import json
from importlib.resources import files as pkg_files
from pathlib import Path

import pytest

from durin.utils.helpers import seed_workflows
from durin.workflow.spec import parse_workflow

_SEED_NAMES = [
    "research-to-answer",
    "brainstorming",
    "writing-plans",
    "build-specs",
    "execute-plan",
    "debug",
    "review-changes",
]


def _seed_path(name: str) -> Path:
    tpl = pkg_files("durin") / "templates" / "workflows"
    return Path(str(tpl / f"{name}.json"))


def _load_seed(name: str):
    """Load and parse a seed workflow by name."""
    path = _seed_path(name)
    data = json.loads(path.read_text(encoding="utf-8"))
    return parse_workflow(data)


@pytest.mark.parametrize("name", _SEED_NAMES)
def test_seed_file_exists(name: str):
    path = _seed_path(name)
    assert path.exists(), f"seed file not found: {name}.json"


@pytest.mark.parametrize("name", _SEED_NAMES)
def test_seed_parses(name: str):
    path = _seed_path(name)
    data = json.loads(path.read_text(encoding="utf-8"))
    wf = parse_workflow(data)
    assert wf.name == name


@pytest.mark.parametrize("name", _SEED_NAMES)
def test_seed_has_description(name: str):
    # Every seed carries a non-empty top-level one-line description: it is the
    # discovery hint list_workflows surfaces so an agent can pick which to run.
    data = json.loads(_seed_path(name).read_text(encoding="utf-8"))
    desc = data.get("description")
    assert isinstance(desc, str) and desc.strip(), (
        f"seed {name!r} is missing a non-empty top-level 'description'"
    )


def test_seed_workflows_copies_all(tmp_path: Path):
    added = seed_workflows(tmp_path)
    dest = tmp_path / "workflows"
    for name in _SEED_NAMES:
        assert (dest / f"{name}.json").exists(), f"missing: {name}.json"
    assert len(added) == len(_SEED_NAMES)


def test_seed_workflows_idempotent(tmp_path: Path):
    seed_workflows(tmp_path)
    # second run must not add anything
    added2 = seed_workflows(tmp_path)
    assert added2 == []


def test_seed_workflows_does_not_overwrite_existing(tmp_path: Path):
    seed_workflows(tmp_path)
    target = tmp_path / "workflows" / "research-to-answer.json"
    target.write_text("USER_CONTENT", encoding="utf-8")
    seed_workflows(tmp_path)
    assert target.read_text(encoding="utf-8") == "USER_CONTENT"


@pytest.mark.parametrize("name", _SEED_NAMES)
def test_seed_does_not_hardcode_a_model_or_persona(name: str):
    # A seed must not pin a specific model/judge_model/persona: those are user-
    # environment choices. Omitting them lets every node inherit the engine default,
    # so a seed runs on whatever provider the user has configured (a hardcoded
    # 'glm-4.7' would simply fail for a user without that provider).
    data = json.loads(_seed_path(name).read_text(encoding="utf-8"))
    for node in data.get("nodes", []):
        for field in ("model", "judge_model", "persona"):
            assert field not in node, (
                f"seed {name!r} node {node.get('id')!r} hardcodes {field!r}="
                f"{node.get(field)!r}; omit it so the node uses the default"
            )


def test_looping_seed_producers_use_persistent_sessions():
    # Nodes that loop back (self-loop or targeted by on_fail) must use persistent
    # sessions to maintain context across re-entries. This test verifies the looping
    # producers in seed workflows are configured correctly.
    for name, nodes in [("execute-plan", ["implement"]), ("debug", ["diagnose"])]:
        wf = _load_seed(name)
        for node_id in nodes:
            assert wf.nodes[node_id].session == "persistent", f"{name}:{node_id}"
