"""SP1: SkillsService — focused unit tests covering read, write, async, and error paths.

These tests call the service directly (no HTTP) using a tmp skills workspace.
A successful call returns a ``SkillsResult`` (2xx, ``.data`` only); a non-2xx
store outcome is raised as a DomainError (payload echoed in ``.details``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.service.principal import Principal, Scope
from durin.service.skills import (
    GithubTokenTestQuery,
    SkillApproveCommand,
    SkillGetQuery,
    SkillJudgeQuery,
    SkillModeCommand,
    SkillRejectCommand,
    SkillRemoveCommand,
    SkillReviewCommand,
    SkillSaveCommand,
    SkillUnreviewCommand,
    SkillsImportCommand,
    SkillsListQuery,
    SkillsQuarantineQuery,
    SkillsResolveQuery,
    SkillsService,
)
from durin.service.types import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)


def _make_workspace(tmp_path: Path) -> Path:
    """Return a tmp workspace with one installed skill with provenance.

    ``sweep_unverified_skills`` moves skills without a provenance block to
    quarantine.  Adding a ``durin.provenance`` block prevents that sweep so
    the skill stays in the skills list.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    skill_dir = ws / "skills" / "hello"
    skill_dir.mkdir(parents=True)
    # provenance must live at metadata.durin.provenance (see _durin_blob / ensure_durin)
    content = (
        "---\n"
        "name: hello\n"
        "description: greet\n"
        "mode: manual\n"
        "metadata:\n"
        "  durin:\n"
        "    provenance:\n"
        "      source: test\n"
        "      verdict: safe\n"
        "---\n"
        "Say hello.\n"
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return ws


def _make_quarantine(ws: Path, name: str = "pending") -> Path:
    """Seed a skill in the quarantine dir with a safe scan result."""
    qdir = ws / ".durin" / "import-quarantine" / name
    qdir.mkdir(parents=True)
    (qdir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    (qdir / ".scan.json").write_text(
        json.dumps({"source": f"local:{name}", "verdict": "safe", "findings": []}),
        encoding="utf-8",
    )
    return qdir


def _svc(ws: Path) -> SkillsService:
    return SkillsService(workspace=ws)


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


async def test_list_returns_skills(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    result = await svc.list(SkillsListQuery(), Principal.local())
    assert "skills" in result.data
    names = {s["name"] for s in result.data["skills"]}
    assert "hello" in names


async def test_list_requires_read_scope(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await svc.list(SkillsListQuery(), principal)


async def test_quarantine_returns_quarantined_list(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _make_quarantine(ws, "pending")
    svc = _svc(ws)
    result = await svc.quarantine(SkillsQuarantineQuery(), Principal.local())
    assert "quarantined" in result.data
    names = {s["name"] for s in result.data["quarantined"]}
    assert "pending" in names


async def test_get_existing_skill(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    result = await svc.get(SkillGetQuery(name="hello"), Principal.local())
    assert result.data["name"] == "hello"


async def test_get_missing_skill_raises_not_found(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    with pytest.raises(NotFoundError) as exc:
        await svc.get(SkillGetQuery(name="no-such"), Principal.local())
    assert "error" in exc.value.details


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


async def test_save_overwrites_skill_content(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    result = await svc.save(
        SkillSaveCommand(name="hello", content="---\nname: hello\ndescription: updated\n---\nNew body.\n"),
        Principal.local(),
    )
    assert result.data.get("ok") or result.data.get("name") == "hello"


async def test_save_requires_write_scope(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    read_only = Principal.remote("t", frozenset({Scope.SKILLS_READ.value}))
    with pytest.raises(ForbiddenError):
        await svc.save(
            SkillSaveCommand(name="hello", content="---\nname: hello\n---\nbody\n"),
            read_only,
        )


async def test_mode_changes_skill_mode(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    result = await svc.mode(SkillModeCommand(name="hello", value="auto"), Principal.local())
    assert "error" not in result.data


async def test_reject_removes_quarantine_dir(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    _make_quarantine(ws, "toreject")
    svc = _svc(ws)
    result = await svc.reject(SkillRejectCommand(name="toreject"), Principal.local())
    assert result.data.get("ok") is True
    assert not (ws / ".durin" / "import-quarantine" / "toreject").exists()


async def test_remove_installed_skill(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    result = await svc.remove(SkillRemoveCommand(name="hello"), Principal.local())
    assert result.data.get("ok") is True
    assert not (ws / "skills" / "hello").exists()


async def test_review_marks_active_skill(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    result = await svc.review(SkillReviewCommand(name="hello", note="ok"), Principal.local())
    assert result.data.get("reviewed") is True


async def test_review_requires_write_scope(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    read_only = Principal.remote("t", frozenset({Scope.SKILLS_READ.value}))
    with pytest.raises(ForbiddenError):
        await svc.review(SkillReviewCommand(name="hello"), read_only)


async def test_unreview_clears_review(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    await svc.review(SkillReviewCommand(name="hello"), Principal.local())
    result = await svc.unreview(SkillUnreviewCommand(name="hello"), Principal.local())
    assert result.data.get("reviewed") is False


# ---------------------------------------------------------------------------
# Async path
# ---------------------------------------------------------------------------


async def test_judge_runs_on_quarantined_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _make_workspace(tmp_path)
    _make_quarantine(ws, "cand")
    monkeypatch.setattr(
        "durin.memory.llm_invoke.judge_llm_invoke",
        lambda prompt, *, model=None: "===FINDINGS===\n===END===\n",
    )
    svc = _svc(ws)
    result = await svc.judge(SkillJudgeQuery(name="cand"), Principal.local())
    assert result.data.get("judged") is True


# ---------------------------------------------------------------------------
# Error paths — non-2xx store outcomes are raised as DomainErrors
# ---------------------------------------------------------------------------


async def test_approve_without_confirm_raises_conflict(tmp_path: Path) -> None:
    """The store returns status=409 when confirm is not set for a safe skill —
    surfaced as a ConflictError with the gate payload in ``details``."""
    ws = _make_workspace(tmp_path)
    src = tmp_path / "src" / "newskill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: newskill\ndescription: d\n---\nbody\n", encoding="utf-8")

    svc = _svc(ws)
    imp = await svc.import_skill(SkillsImportCommand(source=str(src)), Principal.local())
    assert imp.data is not None

    with pytest.raises(ConflictError) as exc:
        await svc.approve(
            SkillApproveCommand(name="newskill", confirm=False), Principal.local()
        )
    assert "refused" in exc.value.details


async def test_approve_with_install_deps_does_not_500(tmp_path: Path) -> None:
    """Regression: approve with install_deps=True (what the webui's approve button
    sends) built the exec tool ctx from the top-level Config, but ExecTool.create
    reads ctx.config.exec — a ToolsConfig field — so it raised AttributeError →
    HTTP 500. _get_exec_run must hand ExecTool the tools sub-config."""
    ws = _make_workspace(tmp_path)
    _make_quarantine(ws, "cand")
    svc = _svc(ws)
    result = await svc.approve(
        SkillApproveCommand(name="cand", confirm=True, install_deps=True),
        Principal.local(),
    )
    assert result.data.get("ok") is True
    assert result.data.get("deps_results") == []  # no specs → empty, never a 500


async def test_resolve_lists_local_candidates(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    src = tmp_path / "src" / "myskill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: myskill\ndescription: d\n---\nbody\n", encoding="utf-8")

    svc = _svc(ws)
    result = await svc.resolve(SkillsResolveQuery(source=str(src)), Principal.local())
    assert "candidates" in result.data


async def test_github_token_test_missing_name_raises_validation(
    tmp_path: Path,
) -> None:
    """An empty secret name is rejected by the store with status 400 →
    ValidationFailedError (422)."""
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    with pytest.raises(ValidationFailedError) as exc:
        await svc.github_token_test(GithubTokenTestQuery(secret=""), Principal.local())
    assert "error" in exc.value.details


async def test_github_token_test_unknown_secret_returns_ok_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named secret that fails resolution returns ok=False (status 200)."""
    monkeypatch.setattr(
        "durin.security.secrets.resolve_secret",
        lambda ref: (_ for _ in ()).throw(Exception("not found")),
    )
    ws = _make_workspace(tmp_path)
    svc = _svc(ws)
    result = await svc.github_token_test(GithubTokenTestQuery(secret="MYTOKEN"), Principal.local())
    assert result.data["ok"] is False
