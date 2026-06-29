# tests/agent/tools/test_path_utils_work_area.py
from pathlib import Path

import pytest

from durin.agent.tools.path_utils import resolve_workspace_path


def test_plain_relative_resolves_under_work_dir(tmp_path):
    ws = tmp_path
    work = ws / "work" / "s1"
    work.mkdir(parents=True)
    out = resolve_workspace_path("scrape.py", ws, allowed_dir=ws, work_dir=work)
    assert out == (work / "scrape.py").resolve()


def test_managed_prefix_resolves_under_workspace(tmp_path):
    ws = tmp_path
    work = ws / "work" / "s1"
    work.mkdir(parents=True)
    out = resolve_workspace_path("ingested/x/source.md", ws, allowed_dir=ws, work_dir=work)
    assert out == (ws / "ingested" / "x" / "source.md").resolve()


def test_no_work_dir_preserves_legacy_behavior(tmp_path):
    ws = tmp_path
    out = resolve_workspace_path("scrape.py", ws, allowed_dir=ws)
    assert out == (ws / "scrape.py").resolve()


def test_work_dir_write_is_contained(tmp_path):
    ws = tmp_path
    work = ws / "work" / "s1"
    work.mkdir(parents=True)
    # must not raise PermissionError — work dir is inside the allowlist
    resolve_workspace_path("out.json", ws, allowed_dir=ws, work_dir=work)
