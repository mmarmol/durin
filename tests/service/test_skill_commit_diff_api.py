"""Task 2: SkillsService commit-diff endpoint.

Tests the GET /api/v1/skills/{name}/commit/{sha}/diff handler.

Uses the same direct-construction pattern as test_skill_suggestions_api.py:
  SkillsService(workspace=...) + Principal.local()
"""

from __future__ import annotations

import pytest

from durin.agent import skills_store as ss
from durin.service.principal import Principal
from durin.service.skills import SkillCommitDiffQuery, SkillsService


@pytest.mark.asyncio
async def test_commit_diff_endpoint(tmp_path):
    ws = tmp_path
    d = ws / "skills" / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: d\ndurin:\n  mode: manual\n---\nv1\n",
        encoding="utf-8",
    )
    store = ss._store_init(ws)
    store.auto_commit("seed")
    (d / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: d\ndurin:\n  mode: manual\n---\nv2\n",
        encoding="utf-8",
    )
    sha = store.auto_commit("edit")

    svc = SkillsService(workspace=ws)
    pr = Principal.local()
    res = await svc.commit_diff(SkillCommitDiffQuery(name="alpha", sha=sha), pr)
    assert "alpha/SKILL.md" in res.patch
    assert "v2" in res.patch
